"""core_memory — 核心记忆模块

区分核心记忆（长期稳定）和普通记忆（可过期）。
核心记忆不会被 auto_expire 清理，有独立的生命周期。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional, List, Dict

logger = logging.getLogger("mem0x.core_memory")

# SQLite 路径
_db_path: Optional[str] = None
_lock = threading.Lock()


def _get_db_path() -> str:
    global _db_path
    if _db_path is None:
        from security.utils import get_data_dir
        _db_path = os.path.join(get_data_dir(), "core_memory.db")
    return _db_path


def _init_db():
    """初始化 core_memory SQLite 表。"""
    db_path = _get_db_path()
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS core_memories (
                    memory_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    importance REAL DEFAULT 0.5,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cm_category
                ON core_memories(category)
            """)
            conn.commit()
    except Exception as e:
        logger.error("core_memory DB 初始化失败: %s", e)


def _ensure_db():
    """确保 DB 已初始化。"""
    with _lock:
        _init_db()


def add_core_memory(memory_id: str, content: str, category: str = "general",
                    importance: float = 0.5) -> bool:
    """将记忆标记为核心记忆。"""
    _ensure_db()
    try:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO core_memories
                (memory_id, content, category, importance, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
            """, (memory_id, content, category, importance))
            conn.commit()
        logger.info("已标记为核心记忆: %s", memory_id[:16])
        return True
    except Exception as e:
        logger.error("添加核心记忆失败: %s", e)
        return False


def remove_core_memory(memory_id: str) -> bool:
    """移除核心记忆标记（降级为普通记忆）。"""
    _ensure_db()
    try:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute("DELETE FROM core_memories WHERE memory_id = ?", (memory_id,))
            conn.commit()
        logger.info("已移除核心记忆标记: %s", memory_id[:16])
        return True
    except Exception as e:
        logger.error("移除核心记忆失败: %s", e)
        return False


def is_core_memory(memory_id: str) -> bool:
    """检查是否为核心记忆。"""
    _ensure_db()
    try:
        with sqlite3.connect(_get_db_path()) as conn:
            cursor = conn.execute(
                "SELECT 1 FROM core_memories WHERE memory_id = ?", (memory_id,)
            )
            return cursor.fetchone() is not None
    except Exception as e:
        logger.error("查询核心记忆失败: %s", e)
        return False


def list_core_memories(category: Optional[str] = None, limit: int = 100) -> List[Dict]:
    """列出核心记忆。"""
    _ensure_db()
    try:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            if category:
                cursor = conn.execute(
                    "SELECT * FROM core_memories WHERE category = ? ORDER BY importance DESC LIMIT ?",
                    (category, limit)
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM core_memories ORDER BY importance DESC LIMIT ?",
                    (limit,)
                )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error("列出核心记忆失败: %s", e)
        return []


def get_core_memory(memory_id: str) -> Optional[Dict]:
    """获取单条核心记忆详情。"""
    _ensure_db()
    try:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM core_memories WHERE memory_id = ?", (memory_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error("获取核心记忆失败: %s", e)
        return None


def update_importance(memory_id: str, importance: float) -> bool:
    """更新核心记忆的重要性分数。"""
    _ensure_db()
    try:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute("""
                UPDATE core_memories
                SET importance = ?, updated_at = datetime('now')
                WHERE memory_id = ?
            """, (importance, memory_id))
            conn.commit()
        return True
    except Exception as e:
        logger.error("更新重要性失败: %s", e)
        return False


# 初始化
_ensure_db()
