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
    except Exception as e:
        logger.warning("load salience config: %s", e)
        return {}


def _compute_fsrs_retrievability(memory_id: str) -> Optional[float]:
    """尝试从 Qdrant 获取 metadata，计算 FSRS retrievability。
    无 fsrs_card 时返回 None。"""
    try:
        from wrapper.mem0_runtime import get_memory
        from wrapper.fsrs_bridge import compute_retrievability
        import asyncio as _asyncio

        mem = get_memory()
        if not mem:
            return None

        async def _fetch():
            got = await mem.get(memory_id)
            if not got:
                return None
            metadata = got.get("metadata") or {}
            if not metadata.get("fsrs_card"):
                return None
            created_at = got.get("created_at") or metadata.get("created_at")
            return compute_retrievability(metadata, created_at)

        try:
            return _asyncio.run(_fetch())
        except RuntimeError:
            loop = _asyncio.new_event_loop()
            try:
                return loop.run_until_complete(_fetch())
            finally:
                loop.close()
    except Exception as e:
        logger.warning("compute FSRS retrievability: %s", e)
        return None


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
    """记忆被搜索命中时，用 FSRS 计算 retrievability 并缓存。无 fsrs_card 时 fallback 到简单 boost。"""
    _ensure_schema()
    cfg = _get_config()
    boost = cfg.get("access_boost", DEFAULT_ACCESS_BOOST)
    now = time.time()

    # 尝试 FSRS retrievability
    fsrs_val = _compute_fsrs_retrievability(memory_id)

    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT salience, access_count FROM salience WHERE memory_id=?",
            (memory_id,),
        ).fetchone()

        if row:
            cnt = row["access_count"]
            if fsrs_val is not None:
                new_s = fsrs_val
            else:
                # fallback: 简单 boost
                new_s = min(1.0, row["salience"] + boost)
            conn.execute(
                "UPDATE salience SET salience=?, last_access=?, access_count=? WHERE memory_id=?",
                (new_s, now, cnt + 1, memory_id),
            )
        else:
            if fsrs_val is not None:
                new_s = fsrs_val
            else:
                new_s = cfg.get("initial_value", DEFAULT_INITIAL)
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
    """获取单条记忆的当前 salience（已是 FSRS 计算后的缓存值）。"""
    _ensure_schema()
    cfg = _get_config()

    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT salience FROM salience WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        if not row:
            return cfg.get("initial_value", DEFAULT_INITIAL)
        return row["salience"]
    finally:
        conn.close()


def get_batch_salience(memory_ids: List[str]) -> Dict[str, float]:
    """批量获取 salience（已是 FSRS 计算后的缓存值）。"""
    if not memory_ids:
        return {}

    _ensure_schema()
    cfg = _get_config()
    initial = cfg.get("initial_value", DEFAULT_INITIAL)

    conn = _get_db()
    try:
        placeholders = ",".join("?" * len(memory_ids))
        rows = conn.execute(
            f"SELECT memory_id, salience FROM salience WHERE memory_id IN ({placeholders})",
            memory_ids,
        ).fetchall()

        result = {}
        for r in rows:
            result[r["memory_id"]] = r["salience"]

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
    
    同时更新 FSRS card 状态（记录访问），统一遗忘模型，并持久化到 Qdrant。
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

    fsrs_updates = []  # 收集需要持久化的 FSRS card 更新
    for r in results:
        mid = r.get("id")
        if mid and mid in salience_map:
            r["heat"] = salience_map[mid]
        # 更新 FSRS card
        if _fsrs_available and mid:
            try:
                metadata = r.get("metadata") or {}
                if metadata.get("fsrs_card"):
                    new_meta = fsrs_record_access(metadata)
                    r["metadata"] = {**metadata, **new_meta}
                    fsrs_updates.append((mid, new_meta))
            except Exception as e:
                logger.debug("FSRS record_access failed: %s", e)

    # working_memory TTL 刷新：搜索命中时重置 accessed_at
    try:
        from wrapper.working_memory import touch as wm_touch
        wm_cfg = {}
        try:
            from security.utils import get_config
            wm_cfg = get_config().get("working_memory", {})
        except Exception as e:
            logger.debug("load working_memory config failed: %s", e)
        wm_ttl = wm_cfg.get("default_ttl_days", 90)
        for mid in ids:
            if mid:
                try:
                    wm_touch(mid, ttl_days=wm_ttl)
                except Exception as e:
                    logger.debug("working_memory touch failed for %s: %s", mid[:16], e)
    except ImportError:
        pass

    # 持久化 FSRS card 到 Qdrant（批量，单次 HTTP）
    if fsrs_updates:
        try:
            import asyncio as _asyncio
            from wrapper.mem0_runtime import get_memory
            mem = get_memory()
            if mem:
                async def _persist_fsrs():
                    for mid, new_meta in fsrs_updates:
                        try:
                            got = await mem.get(mid)
                            if got:
                                old_meta = got.get("metadata") or {}
                                merged = {**old_meta, **new_meta}
                                await mem.update(mid, got.get("memory") or got.get("content") or "", metadata=merged)
                        except Exception as e:
                                logger.debug("persist FSRS card for %s failed: %s", mid[:16], e)
                try:
                    _asyncio.run(_persist_fsrs())
                except RuntimeError:
                    _loop = _asyncio.new_event_loop()
                    try:
                        _loop.run_until_complete(_persist_fsrs())
                    finally:
                        _loop.close()
        except Exception as e:
            logger.warning("persist FSRS batch update: %s", e)

    return results


def batch_on_memory_accessed(memory_ids: list) -> None:
    """批量更新 salience：优先 FSRS retrievability，无 fsrs_card 时 fallback 简单 boost。"""
    _ensure_schema()
    cfg = _get_config()
    boost = cfg.get("access_boost", DEFAULT_ACCESS_BOOST)
    now = time.time()

    # 批量预取 FSRS retrievability
    fsrs_map: Dict[str, Optional[float]] = {}
    for mid in memory_ids:
        if mid:
            fsrs_map[mid] = _compute_fsrs_retrievability(mid)

    conn = _get_db()
    try:
        for memory_id in memory_ids:
            if not memory_id:
                continue
            try:
                fsrs_val = fsrs_map.get(memory_id)
                row = conn.execute(
                    "SELECT salience, access_count FROM salience WHERE memory_id=?",
                    (memory_id,),
                ).fetchone()
                if row:
                    cnt = row["access_count"]
                    if fsrs_val is not None:
                        new_s = fsrs_val
                    else:
                        new_s = min(1.0, row["salience"] + boost)
                    conn.execute(
                        "UPDATE salience SET salience=?, last_access=?, access_count=? WHERE memory_id=?",
                        (new_s, now, cnt + 1, memory_id),
                    )
                else:
                    if fsrs_val is not None:
                        new_s = fsrs_val
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
