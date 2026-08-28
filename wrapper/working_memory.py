"""working_memory — 工作记忆层（SQLite）

为每个 user_id 维护一份轻量级工作记忆（高频上下文）。
搜索时作为 high-priority 候选注入，TTL 到期自动清理。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Dict, List, Optional

logger = logging.getLogger("mem0x.working_memory")

# ── Schema ──────────────────────────────────────────────────
_WORKING_MEMORY_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS working_memory (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id     TEXT NOT NULL,
        user_id       TEXT NOT NULL,
        content       TEXT NOT NULL,
        access_count  INTEGER NOT NULL DEFAULT 0,
        created_at    REAL NOT NULL,
        accessed_at   REAL NOT NULL,
        ttl_days      INTEGER NOT NULL DEFAULT 90
    )""",
    "CREATE INDEX IF NOT EXISTS idx_wm_user ON working_memory(user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_wm_mid ON working_memory(memory_id)",
]
_schema_checked: Dict[str, bool] = {"working_memory": False}

# ── Config defaults ─────────────────────────────────────────
_DEFAULT_CONFIG = {"enabled": True, "default_ttl_days": 90, "injection_weight": 1.5}


def _get_config() -> dict:
    """从 config.json 读取 working_memory 配置。"""
    try:
        from security.utils import get_config
        return get_config().get("working_memory", {})
    except Exception as e:
        logger.warning("working_memory config load: %s", e)
        return {}


def _ensure_schema() -> None:
    from security.db_common import ensure_schema
    ensure_schema("working_memory", _WORKING_MEMORY_SCHEMA, _schema_checked)


def _get_db() -> sqlite3.Connection:
    """获取工作记忆 DB 连接（busy_timeout=5000ms，与 salience 隔离）。"""
    from security.db_common import get_db_path
    db_path = get_db_path("working_memory")
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def add(
    memory_id: str,
    content: str,
    user_id: str,
    ttl_days: Optional[int] = None,
) -> bool:
    """写入工作记忆。memory_id 为 Qdrant ID。"""
    cfg = _get_config()
    if not cfg.get("enabled", _DEFAULT_CONFIG["enabled"]):
        return False

    if ttl_days is None:
        ttl_days = cfg.get("default_ttl_days", _DEFAULT_CONFIG["default_ttl_days"])

    _ensure_schema()
    now = time.time()
    conn = _get_db()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO working_memory
               (memory_id, user_id, content, access_count, created_at, accessed_at, ttl_days)
               VALUES (?, ?, ?, 0, ?, ?, ?)""",
            (memory_id, user_id, content, now, now, ttl_days),
        )
        conn.commit()
        logger.info("working_memory add: user=%s mid=%s ttl=%dd", user_id, memory_id[:16], ttl_days)
        return True
    except Exception as e:
        logger.debug("working_memory add 失败: %s", e)
        return False
    finally:
        conn.close()


def list_items(user_id: str) -> List[Dict]:
    """获取用户的工作记忆列表。"""
    _ensure_schema()
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM working_memory WHERE user_id=? ORDER BY accessed_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def touch(memory_id: str, ttl_days: int = 90) -> None:
    """访问时重置 TTL + access_count++。"""
    _ensure_schema()
    now = time.time()
    conn = _get_db()
    try:
        conn.execute(
            """UPDATE working_memory
               SET accessed_at=?, access_count=access_count+1, ttl_days=?
               WHERE memory_id=?""",
            (now, ttl_days, memory_id),
        )
        conn.commit()
    except Exception as e:
        logger.debug("working_memory touch 失败: %s", e)
    finally:
        conn.close()


def delete_by_memory_id(memory_id: str) -> None:
    """删除工作记忆（联动用）。"""
    _ensure_schema()
    conn = _get_db()
    try:
        conn.execute("DELETE FROM working_memory WHERE memory_id=?", (memory_id,))
        conn.commit()
    except Exception as e:
        logger.debug("working_memory delete 失败: %s", e)
    finally:
        conn.close()


def clear(user_id: Optional[str] = None) -> int:
    """清空工作记忆。user_id=None 时清空全部。"""
    _ensure_schema()
    conn = _get_db()
    try:
        if user_id:
            cur = conn.execute("DELETE FROM working_memory WHERE user_id=?", (user_id,))
        else:
            cur = conn.execute("DELETE FROM working_memory")
        conn.commit()
        return cur.rowcount
    except Exception as e:
        logger.debug("working_memory clear 失败: %s", e)
        return 0
    finally:
        conn.close()


def gc_expired() -> int:
    """清理 TTL 过期的记录。"""
    _ensure_schema()
    now = time.time()
    conn = _get_db()
    try:
        cur = conn.execute(
            "DELETE FROM working_memory WHERE (accessed_at + ttl_days * 86400) < ?",
            (now,),
        )
        conn.commit()
        deleted = cur.rowcount
        if deleted:
            logger.info("working_memory gc: 清理 %d 条过期记录", deleted)
        return deleted
    except Exception as e:
        logger.debug("working_memory gc 失败: %s", e)
        return 0
    finally:
        conn.close()


def stats() -> Dict:
    """返回统计信息。"""
    _ensure_schema()
    now = time.time()
    conn = _get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM working_memory").fetchone()[0]
        expired = conn.execute(
            "SELECT COUNT(*) FROM working_memory WHERE (accessed_at + ttl_days * 86400) < ?",
            (now,),
        ).fetchone()[0]
        return {"total_items": total, "expired_pending_gc": expired, "db": "working_memory.db"}
    except Exception as e:
        logger.debug("working_memory stats 失败: %s", e)
        return {"total_items": 0, "expired_pending_gc": 0, "error": str(e)}
    finally:
        conn.close()
