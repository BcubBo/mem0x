"""FTS5 存储层 — 全文检索 + 热词统计 + 搜索历史 + 自动标签。

所有行为由 config-compose.json 的 fts5 段控制，不写死。
"""

import json
import logging
import os
import re
import sqlite3
import time
from collections import Counter
from typing import Any

logger = logging.getLogger("fts5")

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+")

# 默认配置（config 没配时兜底）
_DEFAULTS = {
    "db_path": "data/fts5.db",
    "tokenizer": "unicode61",
    "bm25_weight": 0.3,
    "highlight": True,
    "hot_words": True,
    "query_log": True,
    "extract_tags": True,
    "search_limit": 20,
    "hot_words_min_length": 2,
}


def _load_config() -> dict:
    """从环境变量指定的配置文件中读取 fts5 段。"""
    config_path = os.environ.get("MEM0X_CONFIG", "config.json")
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        return {**_DEFAULTS, **cfg.get("fts5", {})}
    except Exception as e:
        logger.debug("fts5 config load: %s", e)
        return dict(_DEFAULTS)


class FTS5Store:
    """SQLite FTS5 全文索引存储。"""

    def __init__(self, config: dict | None = None):
        if config is None:
            config = _load_config()
        self.config = config
        self.db_path = config["db_path"]
        self.tokenizer = config["tokenizer"]
        self.bm25_weight = config["bm25_weight"]
        self.highlight_enabled = config["highlight"]
        self.hot_words_enabled = config["hot_words"]
        self.query_log_enabled = config["query_log"]
        self.extract_tags_enabled = config["extract_tags"]
        self.search_limit = config["search_limit"]
        self.hot_words_min_len = config["hot_words_min_length"]

        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS fts5_memories USING fts5(
                memory_id UNINDEXED,
                content,
                user_id UNINDEXED,
                tokenize="{self.tokenizer}"
            );
            CREATE TABLE IF NOT EXISTS fts5_meta (
                memory_id TEXT PRIMARY KEY,
                user_id TEXT,
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS fts5_query_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT,
                user_id TEXT,
                result_count INTEGER,
                elapsed_ms INTEGER,
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS fts5_hot_words (
                word TEXT PRIMARY KEY,
                count INTEGER DEFAULT 1,
                last_seen REAL
            );
            """
        )
        self._conn.commit()

    # ── 写入 / 删除 ──

    def write(self, memory_id: str, content: str, user_id: str = "", metadata: str = "{}") -> None:
        now = time.time()
        try:
            self._conn.execute("BEGIN")
            self._conn.execute(
                "INSERT OR REPLACE INTO fts5_meta (memory_id, user_id, created_at) VALUES (?, ?, ?)",
                (memory_id, user_id, now),
            )
            self._conn.execute("DELETE FROM fts5_memories WHERE memory_id = ?", (memory_id,))
            self._conn.execute(
                "INSERT INTO fts5_memories (memory_id, content, user_id) VALUES (?, ?, ?)",
                (memory_id, content, user_id),
            )
            self._conn.execute("COMMIT")
            logger.debug("FTS5 write: id=%s user=%s len=%d", memory_id[:12], user_id, len(content))
        except Exception as e:
            logger.warning("fts5 write failed, rolling back: %s", e)
            self._conn.execute("ROLLBACK")
            raise

    def delete(self, memory_id: str) -> None:
        try:
            self._conn.execute("BEGIN")
            self._conn.execute("DELETE FROM fts5_memories WHERE memory_id = ?", (memory_id,))
            self._conn.execute("DELETE FROM fts5_meta WHERE memory_id = ?", (memory_id,))
            self._conn.execute("COMMIT")
            logger.debug("FTS5 delete: id=%s", memory_id[:12])
        except Exception as e:
            logger.warning("fts5 delete failed, rolling back: %s", e)
            self._conn.execute("ROLLBACK")
            raise

    # ── 搜索（短语 + 前缀 + 高亮）──

    def search(
        self,
        query: str,
        user_id: str = "",
        limit: int | None = None,
        highlight: bool | None = None,
    ) -> list[dict[str, Any]]:
        """FTS5 搜索：支持短语搜索（带引号）和前缀搜索。返回高亮片段。"""
        if not query.strip():
            return []

        if limit is None:
            limit = self.search_limit
        if highlight is None:
            highlight = self.highlight_enabled

        t0 = time.time()
        tokens = _CJK_RE.findall(query)
        if not tokens:
            return []

        # 判断是否包含短语搜索（带引号的部分）
        phrase_matches = re.findall(r'"([^"]+)"', query)
        if phrase_matches:
            fts5_query = " AND ".join(f'"{p}"' for p in phrase_matches)
        else:
            fts5_query = " OR ".join(f'"{t}"*' for t in tokens)

        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            if highlight:
                if user_id:
                    rows = conn.execute(
                        """
                        SELECT memory_id,
                               highlight(fts5_memories, 1, '[', ']') AS content,
                               snippet(fts5_memories, 1, '[', ']', '...', 32) AS snippet,
                               bm25(fts5_memories) AS score
                        FROM fts5_memories
                        WHERE fts5_memories MATCH ? AND user_id = ?
                        ORDER BY score
                        LIMIT ?
                        """,
                        (fts5_query, user_id, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT memory_id,
                               highlight(fts5_memories, 1, '[', ']') AS content,
                               snippet(fts5_memories, 1, '[', ']', '...', 32) AS snippet,
                               bm25(fts5_memories) AS score
                        FROM fts5_memories
                        WHERE fts5_memories MATCH ?
                        ORDER BY score
                        LIMIT ?
                        """,
                        (fts5_query, limit),
                    ).fetchall()
                results = [
                    {"memory_id": r[0], "content": r[1], "snippet": r[2], "score": r[3]}
                    for r in rows
                ]
            else:
                if user_id:
                    rows = conn.execute(
                        """
                        SELECT memory_id, content, bm25(fts5_memories) AS score
                        FROM fts5_memories
                        WHERE fts5_memories MATCH ? AND user_id = ?
                        ORDER BY score
                        LIMIT ?
                        """,
                        (fts5_query, user_id, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT memory_id, content, bm25(fts5_memories) AS score
                        FROM fts5_memories
                        WHERE fts5_memories MATCH ?
                        ORDER BY score
                        LIMIT ?
                        """,
                        (fts5_query, limit),
                    ).fetchall()
                results = [
                    {"memory_id": r[0], "content": r[1], "score": r[2]}
                    for r in rows
                ]
        finally:
            conn.close()

        elapsed_ms = int((time.time() - t0) * 1000)
        logger.info(
            "FTS5 search: query='%s' user=%s tokens=%d results=%d ms=%d",
            query[:30], user_id, len(tokens), len(results), elapsed_ms,
        )

        # 异步记录搜索历史和热词
        if self.query_log_enabled:
            self._log_query(query, user_id, len(results), elapsed_ms)
        if self.hot_words_enabled:
            self._update_hot_words(tokens)

        return results

    # ── 搜索历史日志 ──

    def _log_query(self, query: str, user_id: str, result_count: int, elapsed_ms: int) -> None:
        try:
            self._conn.execute(
                "INSERT INTO fts5_query_log (query, user_id, result_count, elapsed_ms, created_at) VALUES (?, ?, ?, ?, ?)",
                (query, user_id, result_count, elapsed_ms, time.time()),
            )
            self._conn.commit()
        except Exception as e:
            logger.debug("fts5 log query: %s", e)

    def get_query_history(self, user_id: str = "", limit: int = 20) -> list[dict]:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            if user_id:
                rows = conn.execute(
                    "SELECT query, result_count, elapsed_ms, created_at FROM fts5_query_log WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT query, result_count, elapsed_ms, created_at FROM fts5_query_log ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [
                {"query": r[0], "results": r[1], "ms": r[2], "at": r[3]}
                for r in rows
            ]
        finally:
            conn.close()

    # ── 热词统计 ──

    def _update_hot_words(self, tokens: list[str]) -> None:
        try:
            now = time.time()
            for word in tokens:
                if len(word) >= self.hot_words_min_len:
                    self._conn.execute(
                        "INSERT INTO fts5_hot_words (word, count, last_seen) VALUES (?, 1, ?) "
                        "ON CONFLICT(word) DO UPDATE SET count = count + 1, last_seen = ?",
                        (word, now, now),
                    )
            self._conn.commit()
        except Exception as e:
            logger.debug("fts5 update hot words: %s", e)

    def get_hot_words(self, limit: int = 20) -> list[dict]:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            rows = conn.execute(
                "SELECT word, count, last_seen FROM fts5_hot_words ORDER BY count DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [{"word": r[0], "count": r[1], "last_seen": r[2]} for r in rows]
        finally:
            conn.close()

    # ── 自动标签提取 ──

    def extract_tags(self, content: str, top_n: int = 5) -> list[str]:
        if not self.extract_tags_enabled:
            return []
        tokens = _CJK_RE.findall(content)
        stop_words = {"的", "了", "在", "是", "和", "有", "为", "这", "中", "不", "也", "与", "或", "被", "将", "把", "从", "到", "the", "a", "an", "is", "are", "was", "in", "on", "at", "to", "for", "of", "with", "by"}
        filtered = [t for t in tokens if len(t) >= 2 and t.lower() not in stop_words]
        counter = Counter(filtered)
        return [word for word, _ in counter.most_common(top_n)]

    # ── 全量同步 ──

    def sync_from_qdrant(self, records: list[dict[str, Any]]) -> int:
        now = time.time()
        count = 0
        for rec in records:
            memory_id = rec.get("id", "")
            content = str(rec.get("memory") or rec.get("content") or "")
            meta = rec.get("metadata") or {}
            user_id = meta.get("user_id", "") if isinstance(meta, dict) else ""

            self._conn.execute("DELETE FROM fts5_memories WHERE memory_id = ?", (memory_id,))
            self._conn.execute(
                "INSERT INTO fts5_memories (memory_id, content, user_id) VALUES (?, ?, ?)",
                (memory_id, content, user_id),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO fts5_meta (memory_id, user_id, created_at) VALUES (?, ?, ?)",
                (memory_id, user_id, now),
            )
            count += 1
        self._conn.commit()
        self._conn.execute("INSERT INTO fts5_memories(fts5_memories) VALUES('optimize')")
        self._conn.commit()
        return count

    def count(self, user_id: str = "") -> int:
        if user_id:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM fts5_meta WHERE user_id = ?", (user_id,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM fts5_meta").fetchone()
        return row[0] if row else 0


# 单例
_fts5_instance: FTS5Store | None = None


def get_fts5(db_path: str = "data/fts5.db") -> FTS5Store:
    """获取 FTS5Store 单例（db_path 参数已废弃，配置从 config-compose.json 读取）。"""
    global _fts5_instance
    if _fts5_instance is None:
        _fts5_instance = FTS5Store()
    return _fts5_instance
