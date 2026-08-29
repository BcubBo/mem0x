"""working_memory — 工作记忆层（Redis 缓存 + SQLite 持久化）

为每个 user_id 维护一份轻量级工作记忆（高频上下文）。
搜索时作为 high-priority 候选注入，TTL 到期自动清理。

Redis 数据结构：
  wm:item:{memory_id} — HASH（7字段）
  wm:user:{user_id}   — SET（用户所有 memory_id）
  wm:count             — STRING（全局计数器）
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

# ── Redis cache state ───────────────────────────────────────
_redis_pool = None
_redis_client = None
_redis_unavailable = False
_redis_fail_time: float = 0
_REDIS_RETRY_INTERVAL = 30  # seconds


def _get_config() -> dict:
    """从 config.json 读取 working_memory 配置。"""
    try:
        from security.utils import get_config
        return get_config().get("working_memory", {})
    except Exception as e:
        logger.warning("working_memory config load: %s", e)
        return {}


def _get_redis():
    """获取 Redis 连接（ConnectionPool 单例 + 30s 重试 + 自动降级）。"""
    global _redis_pool, _redis_client, _redis_unavailable, _redis_fail_time

    cfg = _get_config()
    if not cfg.get("redis_cache", False):
        return None

    if _redis_client is not None:
        return _redis_client

    if _redis_unavailable:
        if time.time() - _redis_fail_time < _REDIS_RETRY_INTERVAL:
            return None
        _redis_unavailable = False

    try:
        import redis as _redis_mod
        from security.utils import get_config
        rcfg = get_config().get("redis", {})
        host = rcfg.get("host", "127.0.0.1")
        port = rcfg.get("port", 6379)
        db = cfg.get("db_wm", 1)
        _redis_pool = _redis_mod.ConnectionPool(
            host=host, port=port, db=db,
            decode_responses=True,
            socket_timeout=2, socket_connect_timeout=2,
            max_connections=10,
        )
        _redis_client = _redis_mod.Redis(connection_pool=_redis_pool)
        _redis_client.ping()
        logger.info("working_memory redis connected: %s:%s db=%s", host, port, db)
        return _redis_client
    except Exception as e:
        logger.warning("working_memory redis connect: %s, fallback to sqlite", e)
        _redis_pool = None
        _redis_client = None
        _redis_unavailable = True
        _redis_fail_time = time.time()
        return None


def _redis_conn():
    """获取 Redis 客户端单例，失败返回 None。"""
    return _get_redis()


def _key_item(memory_id: str) -> str:
    cfg = _get_config()
    prefix = f"wm{cfg.get('db_wm', 1)}:" if cfg.get("db_wm") else "wm:"
    return f"{prefix}item:{memory_id}"


def _key_user(user_id: str) -> str:
    cfg = _get_config()
    prefix = f"wm{cfg.get('db_wm', 1)}:" if cfg.get("db_wm") else "wm:"
    return f"{prefix}user:{user_id}"


def _key_count() -> str:
    cfg = _get_config()
    prefix = f"wm{cfg.get('db_wm', 1)}:" if cfg.get("db_wm") else "wm:"
    return f"{prefix}count"


def _row_to_dict(row) -> Dict:
    return {
        "memory_id": row["memory_id"],
        "user_id": row["user_id"],
        "content": row["content"],
        "access_count": row["access_count"],
        "created_at": row["created_at"],
        "accessed_at": row["accessed_at"],
        "ttl_days": row["ttl_days"],
    }


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
    """写入工作记忆。memory_id 为 Qdrant ID。双写 Redis + SQLite。"""
    cfg = _get_config()
    if not cfg.get("enabled", _DEFAULT_CONFIG["enabled"]):
        return False

    if ttl_days is None:
        ttl_days = cfg.get("default_ttl_days", _DEFAULT_CONFIG["default_ttl_days"])

    _ensure_schema()
    now = time.time()

    # Redis cache write
    r = _redis_conn()
    redis_written = False
    if r:
        try:
            ttl_sec = ttl_days * 86400 + 86400
            with r.pipeline() as pipe:
                pipe.hset(_key_item(memory_id), mapping={
                    "memory_id": memory_id,
                    "user_id": user_id,
                    "content": content,
                    "access_count": "0",
                    "created_at": str(now),
                    "accessed_at": str(now),
                    "ttl_days": str(ttl_days),
                })
                pipe.expire(_key_item(memory_id), ttl_sec)
                pipe.sadd(_key_user(user_id), memory_id)
                pipe.expire(_key_user(user_id), ttl_sec)
                pipe.incr(_key_count())
                pipe.execute()
            redis_written = True
        except Exception as e:
            logger.debug("working_memory redis add: %s", e)

    # SQLite write
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
        # SQLite 写入失败，回滚 Redis 以保持一致性
        if redis_written and r:
            try:
                with r.pipeline() as pipe:
                    pipe.delete(_key_item(memory_id))
                    pipe.srem(_key_user(user_id), memory_id)
                    pipe.decr(_key_count())
                    pipe.execute()
                logger.info("working_memory add: Redis rollback ok for mid=%s", memory_id[:16])
            except Exception as rb_e:
                logger.warning("working_memory add Redis rollback 失败: %s", rb_e)
        return False
    finally:
        conn.close()


def list_items(user_id: str) -> List[Dict]:
    """获取用户的工作记忆列表。优先 Redis，miss 时回填。"""
    _ensure_schema()

    # Try Redis cache
    r = _redis_conn()
    if r:
        try:
            mids = r.smembers(_key_user(user_id))
            if mids:
                with r.pipeline() as pipe:
                    for mid in mids:
                        pipe.hgetall(_key_item(mid))
                    results = pipe.execute()
                items = [d for d in results if d]
                if items:
                    items.sort(key=lambda x: float(x.get("accessed_at", 0)), reverse=True)
                    return items
        except Exception as e:
            logger.debug("working_memory redis list: %s, fallback", e)

    # SQLite fallback + backfill
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM working_memory WHERE user_id=? ORDER BY accessed_at DESC",
            (user_id,),
        ).fetchall()
        items = [_row_to_dict(row) for row in rows]

        # Backfill Redis
        if r and items:
            try:
                cfg = _get_config()
                ttl_base = cfg.get("default_ttl_days", _DEFAULT_CONFIG["default_ttl_days"])
                with r.pipeline() as pipe:
                    for item in items:
                        ttl_sec = item["ttl_days"] * 86400 + 86400
                        pipe.hset(_key_item(item["memory_id"]), mapping={
                            k: str(v) for k, v in item.items()
                        })
                        pipe.expire(_key_item(item["memory_id"]), ttl_sec)
                        pipe.sadd(_key_user(user_id), item["memory_id"])
                    pipe.expire(_key_user(user_id), ttl_base * 86400 + 86400)
                    pipe.execute()
            except Exception as e:
                logger.debug("working_memory redis backfill: %s", e)

        return items
    finally:
        conn.close()


def touch(memory_id: str, ttl_days: int = 90) -> None:
    """访问时重置 TTL + access_count++。原子更新 Redis + SQLite。"""
    _ensure_schema()
    now = time.time()

    # Redis cache update
    r = _redis_conn()
    if r:
        try:
            key = _key_item(memory_id)
            if r.exists(key):
                with r.pipeline() as pipe:
                    pipe.hincrby(key, "access_count", 1)
                    pipe.hset(key, "accessed_at", str(now))
                    pipe.hset(key, "ttl_days", str(ttl_days))
                    pipe.expire(key, ttl_days * 86400 + 86400)
                    pipe.execute()
        except Exception as e:
            logger.debug("working_memory redis touch: %s", e)

    # SQLite update
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
    """删除工作记忆（联动用）。Redis + SQLite 双删。"""
    _ensure_schema()

    # Find user_id for SREM (try Redis first, then SQLite)
    user_id = None
    r = _redis_conn()
    if r:
        try:
            user_id = r.hget(_key_item(memory_id), "user_id")
        except Exception:
            pass
    if not user_id:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT user_id FROM working_memory WHERE memory_id=?", (memory_id,)
            ).fetchone()
            if row:
                user_id = row["user_id"]
        finally:
            conn.close()

    # Redis delete
    if r and user_id:
        try:
            with r.pipeline() as pipe:
                pipe.delete(_key_item(memory_id))
                pipe.srem(_key_user(user_id), memory_id)
                pipe.decr(_key_count())
                pipe.execute()
        except Exception as e:
            logger.debug("working_memory redis delete: %s", e)

    # SQLite delete
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

    # Redis clear
    r = _redis_conn()
    if r:
        try:
            if user_id:
                mids = r.smembers(_key_user(user_id))
                if mids:
                    with r.pipeline() as pipe:
                        for mid in mids:
                            pipe.delete(_key_item(mid))
                        pipe.delete(_key_user(user_id))
                        pipe.decrby(_key_count(), len(mids))
                        pipe.execute()
            else:
                r.flushdb()
        except Exception as e:
            logger.debug("working_memory redis clear: %s", e)

    # SQLite clear
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
    """清理 TTL 过期的记录。Redis TTL 自动过期，SQLite 兜底。"""
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
    """返回统计信息。total 来自 Redis 计数器，expired 来自 SQLite。"""
    _ensure_schema()
    now = time.time()

    total = 0
    r = _redis_conn()
    if r:
        try:
            val = r.get(_key_count())
            total = int(val) if val else 0
        except Exception as e:
            logger.debug("working_memory redis stats: %s", e)
            r = None  # fall through to SQLite

    conn = _get_db()
    try:
        if total == 0 and r is None:
            total = conn.execute("SELECT COUNT(*) FROM working_memory").fetchone()[0]
        expired = conn.execute(
            "SELECT COUNT(*) FROM working_memory WHERE (accessed_at + ttl_days * 86400) < ?",
            (now,),
        ).fetchone()[0]
        result = {"total_items": total, "expired_pending_gc": expired, "db": "working_memory.db"}
        if r:
            result["cache"] = "redis"
        return result
    except Exception as e:
        logger.debug("working_memory stats 失败: %s", e)
        return {"total_items": 0, "expired_pending_gc": 0, "error": str(e)}
    finally:
        conn.close()
