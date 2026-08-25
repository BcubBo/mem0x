"""FTS5 存储层 — 提供全文检索能力，用于向量召回的补充/回退。"""

import json
import sqlite3
import time
from typing import Any


class FTS5Store:
    """SQLite FTS5 全文索引存储。"""

    def __init__(self, db_path: str = "data/fts5.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS fts5_memories USING fts5(
                memory_id UNINDEXED,
                content,
                user_id UNINDEXED,
                tokenize="unicode61"
            );
            CREATE TABLE IF NOT EXISTS fts5_meta (
                memory_id TEXT PRIMARY KEY,
                user_id TEXT,
                created_at REAL
            );
            """
        )
        self._conn.commit()

    def write(
        self,
        memory_id: str,
        content: str,
        user_id: str = "",
        metadata: str = "{}",
    ) -> None:
        now = time.time()
        self._conn.execute(
            "INSERT OR REPLACE INTO fts5_meta (memory_id, user_id, created_at) VALUES (?, ?, ?)",
            (memory_id, user_id, now),
        )
        # FTS5 content 表用 INSERT OR REPLACE 需要先删后插
        self._conn.execute(
            "DELETE FROM fts5_memories WHERE memory_id = ?", (memory_id,)
        )
        self._conn.execute(
            "INSERT INTO fts5_memories (memory_id, content, user_id) VALUES (?, ?, ?)",
            (memory_id, content, user_id),
        )
        self._conn.commit()

    def delete(self, memory_id: str) -> None:
        self._conn.execute(
            "DELETE FROM fts5_memories WHERE memory_id = ?", (memory_id,)
        )
        self._conn.execute(
            "DELETE FROM fts5_meta WHERE memory_id = ?", (memory_id,)
        )
        self._conn.commit()

    def search(
        self, query: str, user_id: str = "", limit: int = 20
    ) -> list[dict[str, Any]]:
        """FTS5 MATCH 搜索。对每个 token 加 * 前缀匹配，兼容 unicode61 的 CJK 单字分词。"""
        if not query.strip():
            return []

        # 构建 FTS5 查询：每个 token 加 * 前缀，用 OR 连接
        import re as _re
        tokens = _re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", query)
        if not tokens:
            return []
        fts5_query = " OR ".join(f'"{t}"*' for t in tokens)

        # 每次搜索用新连接，避免多线程 WAL 可见性问题
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
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
        finally:
            conn.close()

        return [
            {"memory_id": r[0], "content": r[1], "score": r[2]}
            for r in rows
        ]

    def sync_from_qdrant(self, records: list[dict[str, Any]]) -> int:
        """全量同步。records = [{"id", "memory", "metadata"}]，返回写入条数。"""
        now = time.time()
        count = 0
        for rec in records:
            memory_id = rec.get("id", "")
            content = str(rec.get("memory") or rec.get("content") or "")
            meta = rec.get("metadata") or {}
            user_id = meta.get("user_id", "") if isinstance(meta, dict) else ""

            self._conn.execute(
                "DELETE FROM fts5_memories WHERE memory_id = ?", (memory_id,)
            )
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
    """获取 FTS5Store 单例。"""
    global _fts5_instance
    if _fts5_instance is None:
        _fts5_instance = FTS5Store(db_path)
    return _fts5_instance
