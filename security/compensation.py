"""补偿队列：写入失败时暂存，后台重试。

解决：#30(补偿线程lifespan管理) + #32(队列语义修正) + #34(Qdrant写入重试) + #35(持久化)
"""
import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from typing import Optional, Callable, Any

from security.db_common import get_db, get_db_path

logger = logging.getLogger("mem0x.compensation")

# 补偿队列：{action, content, filters, metadata, retries, next_retry_at}
_queue: deque = deque(maxlen=1000)  # #32: 固定容量，满时拒绝新任务（不丢弃旧任务）
_queue_lock = threading.Lock()
_worker_task: Optional[asyncio.Task] = None
_stop_event = threading.Event()

# 配置
MAX_RETRIES = 5
BASE_DELAY = 2  # 秒
MAX_DELAY = 60  # 秒

# SQLite 持久化路径（仅供 os.path.exists 检查）
_DB_PATH = get_db_path("compensation")


def _init_db():
    """初始化补偿队列表。"""
    conn = get_db("compensation")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS compensation_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL DEFAULT 'add',
            content TEXT NOT NULL,
            filters TEXT NOT NULL,
            metadata TEXT,
            retries INTEGER DEFAULT 0,
            next_retry_at REAL NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    # 迁移：旧表可能没有 action 列
    try:
        conn.execute("ALTER TABLE compensation_queue ADD COLUMN action TEXT NOT NULL DEFAULT 'add'")
    except sqlite3.OperationalError:
        pass  # 列已存在
    conn.commit()
    conn.close()


def _persist_task(task: dict):
    """持久化单个任务到 SQLite，记录行 ID 用于精确删除。"""
    try:
        conn = get_db("compensation")
        cursor = conn.execute(
            "INSERT INTO compensation_queue (action, content, filters, metadata, retries, next_retry_at, created_at) VALUES (?,?,?,?,?,?,?)",
            (task.get("action", "add"), task["content"], json.dumps(task["filters"]), json.dumps(task.get("metadata")),
             task["retries"], task["next_retry_at"], task.get("created_at", time.time())),
        )
        task["row_id"] = cursor.lastrowid
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("补偿队列持久化失败: %s", e)


def _load_persisted():
    """从 SQLite 加载未完成的任务到内存队列。"""
    if not os.path.exists(_DB_PATH):
        return
    try:
        conn = get_db("compensation")
        rows = conn.execute("SELECT action, content, filters, metadata, retries, next_retry_at, created_at FROM compensation_queue").fetchall()
        conn.close()
        for action, content, filters_json, meta_json, retries, next_retry_at, created_at in rows:
            if len(_queue) < _queue.maxlen:
                _queue.append({
                    "action": action or "add",
                    "content": content,
                    "filters": json.loads(filters_json),
                    "metadata": json.loads(meta_json) if meta_json else None,
                    "retries": retries,
                    "next_retry_at": next_retry_at,
                    "created_at": created_at,
                })
        if rows:
            logger.info("补偿队列: 从 SQLite 加载 %d 条待重试任务", len(rows))
    except Exception as e:
        logger.warning("补偿队列加载失败: %s", e)


def _clear_persisted(task_content: str = None, row_id: int = None):
    """清除 SQLite 中的已完成任务。优先用 row_id 精确删除，否则清全部。"""
    try:
        conn = get_db("compensation")
        if row_id:
            conn.execute("DELETE FROM compensation_queue WHERE id = ?", (row_id,))
        elif task_content:
            conn.execute("DELETE FROM compensation_queue WHERE content = ?", (task_content,))
        else:
            conn.execute("DELETE FROM compensation_queue")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("补偿队列清除持久化失败: %s", e)


def enqueue(content: str, filters: dict, metadata: Optional[dict] = None, action: str = "add") -> bool:
    """写入失败时调用，将任务加入补偿队列（持久化到 SQLite）。"""
    task = {
        "action": action,
        "content": content,
        "filters": filters,
        "metadata": metadata,
        "retries": 0,
        "next_retry_at": time.time() + BASE_DELAY,
        "created_at": time.time(),
    }
    with _queue_lock:
        if len(_queue) >= _queue.maxlen:
            logger.warning("补偿队列已满(%d)，拒绝新任务", len(_queue))
            return False
        _queue.append(task)
        _persist_task(task)
        logger.info("补偿队列: +1, 当前深度=%d", len(_queue))
        return True


async def _worker(handlers: dict[str, Callable]):
    """后台重试 worker，指数退避（popleft取任务避免竞态）。handlers 按 action 路由。"""
    while not _stop_event.is_set():
        task = None
        with _queue_lock:
            if _queue and _queue[0]["next_retry_at"] <= time.time():
                task = _queue.popleft()  # 取出而非peek

        if task:
            action = task.get("action", "add")
            handler = handlers.get(action)
            if not handler:
                logger.warning("补偿队列: 未知 action=%s, 丢弃任务", action)
                _clear_persisted(task.get("content"), task.get("row_id"))
                continue
            try:
                result = await handler(task["content"], task["filters"], task.get("metadata"))
                if not result or result.get("action") == "error":
                    _requeue(task)  # 失败则放回
                else:
                    _clear_persisted(task.get("content"), task.get("row_id"))  # 成功则只清该条
                    logger.info("补偿队列: %s 重试成功, 剩余=%d", action, len(_queue))
            except Exception as e:
                _requeue(task)
                logger.debug("补偿队列: %s 重试失败: %s", action, e)
        else:
            await asyncio.sleep(1)


def _requeue(task: dict):
    """失败任务放回队列（重试次数+1）。"""
    _bump_retry(task)
    if task["retries"] < MAX_RETRIES:
        with _queue_lock:
            _queue.appendleft(task)  # 放回队首


def _bump_retry(task: dict):
    """增加重试次数，超过上限则丢弃（同时更新 SQLite 状态）。"""
    task["retries"] += 1
    if task["retries"] >= MAX_RETRIES:
        with _queue_lock:
            try:
                _queue.remove(task)
            except ValueError:
                pass
        _persist_retry_state(task)
        logger.warning("补偿队列: 重试%d次后放弃, 内容前50字: %s", MAX_RETRIES, task["content"][:50])
    else:
        delay = min(BASE_DELAY * (2 ** task["retries"]), MAX_DELAY)
        task["next_retry_at"] = time.time() + delay
        _persist_retry_state(task)


def _persist_retry_state(task: dict):
    """同步重试次数和下次重试时间到 SQLite（按 row_id 或 content 精确更新）。"""
    try:
        conn = get_db("compensation")
        if task.get("row_id"):
            conn.execute(
                "UPDATE compensation_queue SET retries=?, next_retry_at=? WHERE id=?",
                (task["retries"], task["next_retry_at"], task["row_id"]),
            )
        else:
            conn.execute(
                "UPDATE compensation_queue SET retries=?, next_retry_at=? WHERE content=? AND created_at=?",
                (task["retries"], task["next_retry_at"], task["content"], task.get("created_at", 0)),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("补偿队列重试状态持久化失败: %s", e)


def start(write_fn: Callable = None, handlers: Optional[dict[str, Callable]] = None):
    """启动补偿队列 worker（在 lifespan 中调用，初始化 SQLite + 加载持久化任务）。

    handlers: {action: async_callable(content, filters, metadata)} 路由表。
              若未提供则回退到 {"add": write_fn} 保持向后兼容。
    """
    global _worker_task
    _stop_event.clear()
    _init_db()
    _load_persisted()

    if handlers is None:
        if write_fn is None:
            raise ValueError("start() 需要 write_fn 或 handlers 参数")
        handlers = {"add": write_fn}

    async def _run():
        await _worker(handlers)

    loop = asyncio.get_event_loop()
    _worker_task = loop.create_task(_run())
    logger.info("补偿队列 worker 已启动 (actions: %s)", list(handlers.keys()))


def stop():
    """停止补偿队列 worker（在 lifespan 关闭时调用）。"""
    _stop_event.set()
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
    logger.info("补偿队列 worker 已停止, 剩余任务=%d", len(_queue))


def stats() -> dict:
    """返回队列统计信息。"""
    with _queue_lock:
        return {
            "depth": len(_queue),
            "max_size": _queue.maxlen,
            "pending_retries": sum(1 for t in _queue if t["retries"] > 0),
        }


def dead_stats(limit: int = 50) -> dict:
    """查询已放弃的任务（重试耗尽）。从 SQLite 中检索 retries >= MAX_RETRIES 的记录。"""
    result = {"total": 0, "tasks": []}
    if not os.path.exists(_DB_PATH):
        return result
    try:
        conn = get_db("compensation")
        row_count = conn.execute(
            "SELECT COUNT(*) FROM compensation_queue WHERE retries >= ?", (MAX_RETRIES,)
        ).fetchone()[0]
        result["total"] = row_count
        if row_count > 0:
            logger.warning("dead letter queue: %d tasks exceeded max retries", row_count)
        rows = conn.execute(
            "SELECT id, action, content, filters, metadata, retries, next_retry_at, created_at "
            "FROM compensation_queue WHERE retries >= ? ORDER BY created_at DESC LIMIT ?",
            (MAX_RETRIES, limit),
        ).fetchall()
        conn.close()
        for row_id, action, content, filters_json, meta_json, retries, next_retry_at, created_at in rows:
            result["tasks"].append({
                "id": row_id,
                "action": action or "add",
                "content_preview": content[:100] if content else "",
                "retries": retries,
                "created_at": created_at,
            })
    except Exception as e:
        logger.warning("dead_stats 查询失败: %s", e)
    return result
