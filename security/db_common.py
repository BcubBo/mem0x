"""共享 SQLite 工具函数。

统一 _get_db_path / _get_db / _ensure_schema，避免 salience/conflict/version_tracker 三处重复。
"""
import logging
import os
import sqlite3
import threading
from typing import Optional

logger = logging.getLogger("mem0x.security.db_common")
_lock = threading.Lock()


def get_db_path(name: str) -> str:
    """获取指定 SQLite 数据库的路径。"""
    data_dir = os.environ.get("MEM0X_DATA_DIR", "data")
    return os.path.join(data_dir, f"{name}.db")


def get_db(name: str) -> sqlite3.Connection:
    """获取 SQLite 连接（WAL 模式 + 10s 超时）。"""
    db_path = get_db_path(name)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(name: str, schema_sql: list[str], checked_flag: dict) -> None:
    """确保 schema 存在（线程安全，只检查一次）。"""
    if checked_flag.get(name):
        return
    with _lock:
        if checked_flag.get(name):
            return
        conn = get_db(name)
        try:
            for sql in schema_sql:
                conn.execute(sql)
            conn.commit()
            checked_flag[name] = True
        except Exception as e:
            logger.debug("ensure_schema failed for %s: %s", name, e, exc_info=True)
        finally:
            conn.close()
