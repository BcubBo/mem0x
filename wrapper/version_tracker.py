"""version_tracker — 记忆版本追踪

每条记忆的每次 update 操作，旧内容自动存入版本历史。
支持查询版本列表和回滚到指定版本。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("mem0x.version_tracker")

_db_path: Optional[str] = None
_lock = threading.Lock()
_schema_checked = False

def _get_db_path() -> str:
    global _db_path
    if _db_path is None:
        from security.utils import get_data_dir
        _db_path = os.path.join(get_data_dir(), "version_history.db")
    return _db_path

def _ensure_schema() -> None:
    global _schema_checked
    if _schema_checked:
        return
    with _lock:
        if _schema_checked:
            return
        db_path = _get_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        try:
            conn = sqlite3.connect(db_path, timeout=10)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS versions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id   TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    metadata    TEXT DEFAULT '{}',
                    version     INTEGER NOT NULL,
                    created_at  REAL NOT NULL,
                    reason      TEXT DEFAULT 'update'
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ver_mem ON versions(memory_id, version)"
            )
            conn.commit()
            conn.close()
            _schema_checked = True
        except Exception as e:
            logger.warning("version_tracker schema 初始化失败: %s", e)

def save_version(
    memory_id: str,
    content: str,
    metadata: Optional[Dict] = None,
    reason: str = "update",
) -> int:
    """保存当前版本到历史表，返回新版本号。"""
    _ensure_schema()
    import json

    conn = sqlite3.connect(_get_db_path(), timeout=10)
    try:
        # 查当前最大版本号
        row = conn.execute(
            "SELECT MAX(version) FROM versions WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        new_version = (row[0] or 0) + 1

        conn.execute(
            "INSERT INTO versions (memory_id, content, metadata, version, created_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                memory_id,
                content,
                json.dumps(metadata or {}, ensure_ascii=False),
                new_version,
                time.time(),
                reason,
            ),
        )
        conn.commit()
        logger.debug("version_tracker: saved v%d for %s", new_version, memory_id[:8])
        return new_version
    except Exception as e:
        logger.debug("version_tracker save 失败: %s", e)
        return 0
    finally:
        conn.close()


def get_versions(memory_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """查询记忆的版本历史（最新在前）。"""
    _ensure_schema()
    import json

    conn = sqlite3.connect(_get_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM versions WHERE memory_id=? ORDER BY version DESC LIMIT ?",
            (memory_id, limit),
        ).fetchall()

        results = []
        for r in rows:
            meta = {}
            try:
                meta = json.loads(r["metadata"])
            except Exception:
                pass
            results.append({
                "version": r["version"],
                "content": r["content"],
                "metadata": meta,
                "reason": r["reason"],
                "created_at": r["created_at"],
            })
        return results
    except Exception as e:
        logger.debug("version_tracker query 失败: %s", e)
        return []
    finally:
        conn.close()


def get_version_count(memory_id: str) -> int:
    """查询记忆的版本总数。"""
    _ensure_schema()
    conn = sqlite3.connect(_get_db_path(), timeout=10)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM versions WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


def get_total_versions() -> int:
    """查询所有记忆的版本总数。"""
    _ensure_schema()
    conn = sqlite3.connect(_get_db_path(), timeout=10)
    try:
        row = conn.execute("SELECT COUNT(*) FROM versions").fetchone()
        return row[0] if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


def get_version_content(memory_id: str, version: int) -> Optional[Dict[str, Any]]:
    """获取指定版本的内容。"""
    _ensure_schema()
    import json
    conn = sqlite3.connect(_get_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM versions WHERE memory_id=? AND version=?",
            (memory_id, version),
        ).fetchone()
        if not row:
            return None
        return {
            "version": row["version"],
            "content": row["content"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
            "reason": row["reason"],
            "created_at": row["created_at"],
        }
    except Exception as e:
        logger.debug("get_version_content 失败: %s", e)
        return None
    finally:
        conn.close()


def cleanup(memory_id: str) -> int:
    """删除指定记忆的所有版本历史，返回删除条数。"""
    _ensure_schema()
    conn = sqlite3.connect(_get_db_path(), timeout=10)
    try:
        cursor = conn.execute(
            "DELETE FROM versions WHERE memory_id=?",
            (memory_id,),
        )
        conn.commit()
        return cursor.rowcount
    except Exception as e:
        logger.debug("version_tracker cleanup 失败: %s", e)
        return 0
    finally:
        conn.close()
