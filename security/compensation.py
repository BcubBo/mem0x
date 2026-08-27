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

# SQLite 持久化
_DB_PATH = os.path.join(os.environ.get("MEM0X_DATA_DIR", "data"), "compensation.db")


def _init_db():
    """初始化补偿队列表。"""
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS compensation_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            filters TEXT NOT NULL,
            metadata TEXT,
            retries INTEGER DEFAULT 0,
            next_retry_at REAL NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _persist_task(task: dict):
    """持久化单个任务到 SQLite。"""
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=10)
        conn.execute(
            "INSERT INTO compensation_queue (content, filters, metadata, retries, next_retry_at, created_at) VALUES (?,?,?,?,?,?)",
            (task["content"], json.dumps(task["filters"]), json.dumps(task.get("metadata")),
             task["retries"], task["next_retry_at"], task.get("created_at", time.time())),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("补偿队列持久化失败: %s", e)


def _load_persisted():
    """从 SQLite 加载未完成的任务到内存队列。"""
    if not os.path.exists(_DB_PATH):
        return
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=10)
        rows = conn.execute("SELECT content, filters, metadata, retries, next_retry_at, created_at FROM compensation_queue").fetchall()
        conn.close()
        for content, filters_json, meta_json, retries, next_retry_at, created_at in rows:
            if len(_queue) < _queue.maxlen:
                _queue.append({
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


def _clear_persisted(task_content: str = None):
    """清除 SQLite 中的已完成任务。指定内容时只删该条，否则清全部。"""
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=10)
        if task_content:
            conn.execute("DELETE FROM compensation_queue WHERE content = ?", (task_content,))
        else:
            conn.execute("DELETE FROM compensation_queue")
        conn.commit()
        conn.close()
    except Exception:
        pass


def enqueue(content: str, filters: dict, metadata: Optional[dict] = None) -> bool:
    """写入失败时调用，将任务加入补偿队列（持久化到 SQLite）。"""
    task = {
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


async def _worker(write_fn: Callable):
    """后台重试 worker，指数退避（popleft取任务避免竞态）。"""
    while not _stop_event.is_set():
        task = None
        with _queue_lock:
            if _queue and _queue[0]["next_retry_at"] <= time.time():
                task = _queue.popleft()  # 取出而非peek

        if task:
            try:
                result = await write_fn(task["content"], task["filters"], task.get("metadata"))
                if not result or result.get("action") == "error":
                    _requeue(task)  # 失败则放回
                else:
                    _clear_persisted(task.get("content"))  # 成功则只清该条
                    logger.info("补偿队列: 重试成功, 剩余=%d", len(_queue))
            except Exception as e:
                _requeue(task)
                logger.debug("补偿队列: 重试失败: %s", e)
        else:
            await asyncio.sleep(1)


def _requeue(task: dict):
    """失败任务放回队列（重试次数+1）。"""
    _bump_retry(task)
    if task["retries"] < MAX_RETRIES:
        with _queue_lock:
            _queue.appendleft(task)  # 放回队首


def _bump_retry(task: dict):
    """增加重试次数，超过上限则丢弃。"""
    task["retries"] += 1
    if task["retries"] >= MAX_RETRIES:
        with _queue_lock:
            try:
                _queue.remove(task)
            except ValueError:
                pass
        logger.warning("补偿队列: 重试%d次后放弃, 内容前50字: %s", MAX_RETRIES, task["content"][:50])
    else:
        delay = min(BASE_DELAY * (2 ** task["retries"]), MAX_DELAY)
        task["next_retry_at"] = time.time() + delay


def start(write_fn: Callable):
    """启动补偿队列 worker（在 lifespan 中调用，初始化 SQLite + 加载持久化任务）。"""
    global _worker_task
    _stop_event.clear()
    _init_db()
    _load_persisted()

    async def _run():
        await _worker(write_fn)

    loop = asyncio.get_event_loop()
    _worker_task = loop.create_task(_run())
    logger.info("补偿队列 worker 已启动")


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
