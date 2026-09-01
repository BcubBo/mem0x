"""NER 数据管线 — 后台线程，周期性采集 → 标注 → 持久化 → 导出 .spacy 语料。

管线架构：
  ① 数据采集层 — ner_buffer（tags_hook push）
  ② 弱监督标注层 — ner_labeler（规则 + spaCy 银标签）
  ③ 语料导出层 — SQLite → train.spacy + dev.spacy（供独立 trainer 容器消费）

本模块实现 ①+②+③ 的编排：drain buffer → label → 存入 SQLite → 导出 .spacy 语料。
训练由独立的 trainer 容器负责（spacy train CLI），本模块不执行训练。
"""

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any

logger = logging.getLogger("ner_pipeline")

# ── 训练数据 SQLite ──


class NERTrainingStore:
    """持久化存储弱监督标注的训练数据。"""

    def __init__(self, db_path: str = "data/ner_training.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()

    def _create_tables(self) -> None:
        with self._lock:
            # ① 建表（不带 index，避免旧表列不存在导致 executescript 失败）
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS ner_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    entities_json TEXT NOT NULL,
                    entity_count INTEGER DEFAULT 0,
                    source TEXT DEFAULT 'weak_supervision',
                    created_at REAL
                );
            """)
            # ② 迁移：给旧表补缺失列
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(ner_samples)").fetchall()}
            if "created_at" not in cols:
                self._conn.execute("ALTER TABLE ner_samples ADD COLUMN created_at REAL")
                self._conn.execute("UPDATE ner_samples SET created_at = 0 WHERE created_at IS NULL")
                logger.info("ner_store: 迁移添加 created_at 列")
            if "point_id" not in cols:
                self._conn.execute("ALTER TABLE ner_samples ADD COLUMN point_id TEXT")
                logger.info("ner_store: 迁移添加 point_id 列")
            # ③ 索引（迁移后再建，确保列存在）
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ner_samples_created
                    ON ner_samples(created_at)
            """)
            self._conn.commit()

    def save_batch(self, samples: list[dict[str, Any]]) -> int:
        """批量保存标注样本。返回成功条数。

        每条 sample: {"text": "...", "entities": [(start, end, "LABEL"), ...]}
        """
        if not samples:
            return 0
        now = time.time()
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                for s in samples:
                    self._conn.execute(
                        "INSERT INTO ner_samples (text, entities_json, entity_count, source, created_at) "
                        "VALUES (?, ?, ?, 'weak_supervision', ?)",
                        (
                            s["text"],
                            json.dumps(s["entities"], ensure_ascii=False),
                            len(s["entities"]),
                            now,
                        ),
                    )
                self._conn.execute("COMMIT")
                logger.info("ner_store: saved %d samples", len(samples))
                return len(samples)
            except Exception as e:
                self._conn.execute("ROLLBACK")
                logger.warning("ner_store save_batch 失败: %s", e)
                return 0

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM ner_samples").fetchone()
            return row[0] if row else 0

    def get_recent(self, limit: int = 100) -> list[dict]:
        """取最近的训练样本（调试用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text, entities_json, entity_count, created_at "
                "FROM ner_samples ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0], "text": r[1][:200],
                "entities": json.loads(r[2]),
                "entity_count": r[3], "created_at": r[4],
            }
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ── 管线编排 ──

_running = False
_thread: threading.Thread | None = None
_store: NERTrainingStore | None = None

def _get_store() -> NERTrainingStore:
    global _store
    if _store is None:
        _store = NERTrainingStore()
    return _store


def _convert_corpus(min_samples: int = 10) -> bool:
    """从 SQLite 导出 .spacy 语料文件（train.spacy + dev.spacy）到 data/corpus/。

    供独立 trainer 容器通过 spacy train CLI 消费。
    返回 True 表示导出成功。
    """
    try:
        import spacy
        from spacy.tokens import DocBin
    except ImportError:
        logger.error("spacy 未安装，跳过语料导出")
        return False

    store = _get_store()
    with store._lock:
        rows = store._conn.execute(
            "SELECT text, entities_json FROM ner_samples"
        ).fetchall()

    if len(rows) < min_samples:
        logger.debug("样本数 %d < %d，跳过导出", len(rows), min_samples)
        return False

    samples = [{"text": r[0], "entities": json.loads(r[1])} for r in rows]

    import random
    random.shuffle(samples)
    dev_size = max(1, int(len(samples) * 0.1))
    dev_samples = samples[:dev_size]
    train_samples = samples[dev_size:]

    try:
        nlp = spacy.load("zh_core_web_sm")
    except Exception as e:
        logger.error("加载基础模型失败: %s", e)
        return False

    def _build_docbin(nlp, samples_list):
        db = DocBin()
        for s in samples_list:
            text = s.get("text", "")
            if not text:
                continue
            doc = nlp.make_doc(text)
            ents = []
            for start, end, label in s.get("entities", []):
                span = doc.char_span(start, end, label=label, alignment_mode="contract")
                if span is not None:
                    ents.append(span)
            doc.ents = spacy.util.filter_spans(ents)
            db.add(doc)
        return db

    data_dir = os.environ.get("MEM0X_DATA_DIR", "data")
    corpus_dir = os.path.join(data_dir, "corpus")
    os.makedirs(corpus_dir, exist_ok=True)

    train_db = _build_docbin(nlp, train_samples)
    dev_db = _build_docbin(nlp, dev_samples)
    train_db.to_disk(os.path.join(corpus_dir, "train.spacy"))
    dev_db.to_disk(os.path.join(corpus_dir, "dev.spacy"))

    logger.info(
        "语料导出完成: train=%d dev=%d → %s/",
        len(train_samples), len(dev_samples), corpus_dir,
    )
    return True


def _pipeline_loop(get_memory_fn, interval: int = 120) -> None:
    """后台循环：drain → label → save → 导出 .spacy 语料。"""
    global _running
    logger.info("ner_pipeline 启动 (interval=%ds)", interval)

    convert_interval = interval * 5  # 语料导出间隔 = 5倍采集间隔
    last_convert_check = 0.0

    # 延迟导入（避免循环启动时的导入开销）
    from wrapper.ner_buffer import get_buffer
    from wrapper.ner_labeler import label_batch

    while _running:
        try:
            buf = get_buffer()

            # ── 采集阶段：drain → label → save ──
            if len(buf) > 0:
                raw_samples = buf.drain(max_items=100)
                if raw_samples:
                    labeled = label_batch(raw_samples)
                    if labeled:
                        store = _get_store()
                        saved = store.save_batch(labeled)
                        logger.info(
                            "ner_pipeline: drain=%d labeled=%d saved=%d total_db=%d",
                            len(raw_samples), len(labeled), saved, store.count(),
                        )

            # ── 语料导出：独立于 buffer 状态，按时间间隔检查 ──
            now = time.time()
            if now - last_convert_check >= convert_interval:
                last_convert_check = now
                _convert_corpus()

        except Exception as e:
            logger.warning("ner_pipeline 循环异常: %s", e)

        time.sleep(interval)


def start(get_memory_fn, interval: int = 120) -> None:
    """启动 NER 训练管线后台线程。"""
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(
        target=_pipeline_loop,
        args=(get_memory_fn, interval),
        daemon=True,
        name="ner_pipeline",
    )
    _thread.start()
    logger.info("ner_pipeline 线程已启动")


def stop() -> None:
    """停止管线。"""
    global _running, _thread, _store
    _running = False
    if _thread and _thread.is_alive():
        _thread.join(timeout=5)
    _thread = None
    if _store:
        _store.close()
        _store = None
    logger.info("ner_pipeline 已停止")


def trigger_convert(force: bool = False) -> dict[str, Any]:
    """手动触发语料导出（供 API 调用）。返回状态 dict。

    force=True 时跳过样本数量检查（直接导出）。
    """
    total = _get_store().count()
    ok = _convert_corpus(min_samples=0 if force else 10)
    return {
        "exported": ok,
        "force": force,
        "total_samples": total,
    }

# 向后兼容
trigger_train = trigger_convert


def get_status() -> dict:
    """获取 NER 数据管线完整状态（供 API 调用）。"""
    from wrapper.ner_buffer import get_buffer

    store_count = 0
    try:
        store_count = _get_store().count()
    except Exception:
        pass

    # 语料文件状态
    data_dir = os.environ.get("MEM0X_DATA_DIR", "data")
    corpus_dir = os.path.join(data_dir, "corpus")
    corpus_info = {}
    for name in ("train.spacy", "dev.spacy"):
        path = os.path.join(corpus_dir, name)
        if os.path.exists(path):
            corpus_info[name] = {
                "size": os.path.getsize(path),
                "mtime": os.path.getmtime(path),
            }

    return {
        "running": _running,
        "buffer": get_buffer().stats,
        "training_samples": store_count,
        "corpus": corpus_info,
    }


def get_stats() -> dict:
    """管线状态（供 /health 使用，向后兼容）。"""
    return get_status()
