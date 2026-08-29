"""NER 训练管线 — 后台线程，周期性采集 → 标注 → 持久化。

训练管线架构（5层）：
  ① 数据采集层 — ner_buffer（tags_hook push）
  ② 弱监督标注层 — ner_labeler（规则 + spaCy 银标签）  ← 本模块协调
  ③ 训练层 — nlp.update() 增量训练（阶段2）
  ④ 模型管理层 — 版本控制 + 原子替换（阶段2）
  ⑤ 推理层 — spacy_ner.py 从 registry 加载（阶段2）

本模块实现 ①+② 的编排：drain buffer → label → 存入训练数据 SQLite。
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
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS ner_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    entities_json TEXT NOT NULL,
                    entity_count INTEGER DEFAULT 0,
                    source TEXT DEFAULT 'weak_supervision',
                    created_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_ner_samples_created
                    ON ner_samples(created_at);
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


def _pipeline_loop(get_memory_fn, interval: int = 120) -> None:
    """后台循环：drain → label → save。"""
    global _running
    logger.info("ner_pipeline 启动 (interval=%ds)", interval)

    while _running:
        try:
            from wrapper.ner_buffer import get_buffer
            from wrapper.ner_labeler import label_batch

            buf = get_buffer()
            if len(buf) == 0:
                time.sleep(interval)
                continue

            # drain 一批样本
            raw_samples = buf.drain(max_items=100)
            if not raw_samples:
                time.sleep(interval)
                continue

            # 弱监督标注
            labeled = label_batch(raw_samples)
            if labeled:
                store = _get_store()
                saved = store.save_batch(labeled)
                logger.info(
                    "ner_pipeline: drain=%d labeled=%d saved=%d total_db=%d",
                    len(raw_samples), len(labeled), saved, store.count(),
                )
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


def get_stats() -> dict:
    """管线状态（供 /health 使用）。"""
    from wrapper.ner_buffer import get_buffer
    store_count = 0
    try:
        store_count = _get_store().count()
    except Exception:
        pass
    return {
        "running": _running,
        "buffer": get_buffer().stats,
        "training_samples": store_count,
    }
