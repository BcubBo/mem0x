"""salience — 显著性引擎（SQLite，简化版）

- 写入时注册 salience
- 搜索时 boost 访问热度
- 时间衰减
"""
from __future__ import annotations

import logging
import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("mem0x.salience")

# ── 默认配置 ──
DEFAULT_INITIAL = 0.5
DEFAULT_ACCESS_BOOST = 0.1
DEFAULT_DECAY_RATE = 0.023  # 半衰期 ~30 天
DEFAULT_HALF_LIFE_DAYS = 30

_schema_checked = False
_schema_lock = threading.Lock()


# ── 数据库操作（使用 db_common 共享模块）──
_SALIENCE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS salience (
        memory_id      TEXT PRIMARY KEY,
        salience       REAL NOT NULL DEFAULT 0.5,
        last_access    REAL NOT NULL,
        access_count   INTEGER NOT NULL DEFAULT 0,
        created_at     REAL NOT NULL,
        content_preview TEXT DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sal_mem ON salience(memory_id)",
]
_schema_checked = {"salience": False}


def _ensure_schema() -> None:
    from security.db_common import ensure_schema
    ensure_schema("salience", _SALIENCE_SCHEMA, _schema_checked)


def _get_db():
    from security.db_common import get_db
    return get_db("salience")


def _get_config() -> dict:
    """从 config.json 读取 salience 配置。"""
    try:
        from security.utils import get_config
        cfg = get_config()
        return cfg.get("salience", {})
    except Exception:
        return {}


def register(
    memory_id: str,
    content_preview: str = "",
    initial_salience: Optional[float] = None,
) -> None:
    """新记忆写入时注册 salience。"""
    _ensure_schema()
    cfg = _get_config()
    if initial_salience is None:
        initial_salience = cfg.get("initial_value", DEFAULT_INITIAL)

    now = time.time()
    conn = _get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO salience (memory_id, salience, last_access, access_count, created_at, content_preview) VALUES (?,?,?,?,?,?)",
            (memory_id, initial_salience, now, 0, now, content_preview),
        )
        conn.commit()
    except Exception as e:
        logger.debug("salience register 失败: %s", e)
    finally:
        conn.close()


def on_memory_accessed(memory_id: str) -> float:
    """记忆被搜索命中时 boost 显著性。返回新 salience 值。"""
    _ensure_schema()
    cfg = _get_config()
    boost = cfg.get("access_boost", DEFAULT_ACCESS_BOOST)
    decay_rate = cfg.get("decay_rate", DEFAULT_DECAY_RATE)

    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT salience, last_access, access_count FROM salience WHERE memory_id=?",
            (memory_id,),
        ).fetchone()

        if row:
            old_s = row["salience"]
            old_ts = row["last_access"]
            cnt = row["access_count"]
            # 先衰减到当前时刻
            days_elapsed = (time.time() - old_ts) / 86400
            decayed = old_s * math.exp(-decay_rate * days_elapsed)
            # 再 boost
            new_s = min(1.0, decayed + boost)
            conn.execute(
                "UPDATE salience SET salience=?, last_access=?, access_count=? WHERE memory_id=?",
                (new_s, time.time(), cnt + 1, memory_id),
            )
        else:
            # 没有记录，注册一个
            new_s = cfg.get("initial_value", DEFAULT_INITIAL)
            now = time.time()
            conn.execute(
                "INSERT INTO salience (memory_id, salience, last_access, access_count, created_at) VALUES (?,?,?,?,?)",
                (memory_id, new_s, now, 1, now),
            )
        conn.commit()
        return new_s
    except Exception as e:
        logger.debug("salience boost 失败: %s", e)
        return cfg.get("initial_value", DEFAULT_INITIAL)
    finally:
        conn.close()


def get_salience(memory_id: str) -> float:
    """获取单条记忆的当前 salience（含衰减）。"""
    _ensure_schema()
    cfg = _get_config()
    decay_rate = cfg.get("decay_rate", DEFAULT_DECAY_RATE)

    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT salience, last_access FROM salience WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        if not row:
            return cfg.get("initial_value", DEFAULT_INITIAL)
        days_elapsed = (time.time() - row["last_access"]) / 86400
        return row["salience"] * math.exp(-decay_rate * days_elapsed)
    finally:
        conn.close()


def get_batch_salience(memory_ids: List[str]) -> Dict[str, float]:
    """批量获取 salience（消除 N+1 查询）。"""
    if not memory_ids:
        return {}

    _ensure_schema()
    cfg = _get_config()
    decay_rate = cfg.get("decay_rate", DEFAULT_DECAY_RATE)
    initial = cfg.get("initial_value", DEFAULT_INITIAL)

    conn = _get_db()
    try:
        placeholders = ",".join("?" * len(memory_ids))
        rows = conn.execute(
            f"SELECT memory_id, salience, last_access FROM salience WHERE memory_id IN ({placeholders})",
            memory_ids,
        ).fetchall()

        result = {}
        now = time.time()
        for r in rows:
            days_elapsed = (now - r["last_access"]) / 86400
            result[r["memory_id"]] = r["salience"] * math.exp(-decay_rate * days_elapsed)

        # 未找到的用默认值
        for mid in memory_ids:
            if mid not in result:
                result[mid] = initial

        return result
    finally:
        conn.close()


def delete(memory_id: str) -> None:
    """删除 salience 记录。"""
    _ensure_schema()
    conn = _get_db()
    try:
        conn.execute("DELETE FROM salience WHERE memory_id=?", (memory_id,))
        conn.commit()
    except Exception as e:
        logger.debug("salience delete 失败: %s", e)
    finally:
        conn.close()


def boost_salience_for_results(results: List[dict]) -> List[dict]:
    """搜索结果返回后，批量 boost salience 并注入 heat 分数。
    
    同时更新 FSRS card 状态（记录访问），统一遗忘模型。
    """
    if not results:
        return results

    ids = [r.get("id") for r in results if r.get("id")]
    if not ids:
        return results

    # 批量 boost（单连接，减少 N+1）
    batch_on_memory_accessed(ids)

    # 获取更新后的 salience
    salience_map = get_batch_salience(ids)

    # 注入 heat 分数 + 更新 FSRS card
    try:
        from wrapper.fsrs_bridge import record_access as fsrs_record_access
        _fsrs_available = True
    except ImportError:
        _fsrs_available = False

    for r in results:
        mid = r.get("id")
        if mid and mid in salience_map:
            r["heat"] = salience_map[mid]
        # 更新 FSRS card（异步操作，fire-and-forget）
        if _fsrs_available and mid:
            try:
                metadata = r.get("metadata") or {}
                if metadata.get("fsrs_card"):
                    new_meta = fsrs_record_access(metadata)
                    r["metadata"] = {**metadata, **new_meta}
            except Exception:
                pass

    return results


def batch_on_memory_accessed(memory_ids: list) -> None:
    """批量 boost 显著性（单连接，减少 N+1）。"""
    _ensure_schema()
    cfg = _get_config()
    boost = cfg.get("access_boost", DEFAULT_ACCESS_BOOST)
    decay_rate = cfg.get("decay_rate", DEFAULT_DECAY_RATE)
    now = time.time()

    conn = _get_db()
    try:
        for memory_id in memory_ids:
            if not memory_id:
                continue
            try:
                row = conn.execute(
                    "SELECT salience, last_access, access_count FROM salience WHERE memory_id=?",
                    (memory_id,),
                ).fetchone()
                if row:
                    old_s = row["salience"]
                    old_ts = row["last_access"]
                    cnt = row["access_count"]
                    days_elapsed = (now - old_ts) / 86400
                    decayed = old_s * math.exp(-decay_rate * days_elapsed)
                    new_s = min(1.0, decayed + boost)
                    conn.execute(
                        "UPDATE salience SET salience=?, last_access=?, access_count=? WHERE memory_id=?",
                        (new_s, now, cnt + 1, memory_id),
                    )
                else:
                    new_s = cfg.get("initial_value", DEFAULT_INITIAL)
                    conn.execute(
                        "INSERT INTO salience (memory_id, salience, last_access, access_count, created_at) VALUES (?,?,?,?,?)",
                        (memory_id, new_s, now, 1, now),
                    )
            except Exception as e:
                logger.debug("salience batch boost 失败 %s: %s", memory_id[:16], e)
        conn.commit()
    finally:
        conn.close()
