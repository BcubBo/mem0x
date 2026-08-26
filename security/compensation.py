"""补偿队列：写入失败时暂存，后台重试。

解决：#30(补偿线程lifespan管理) + #32(队列语义修正) + #34(Qdrant/Neo4j写入重试)
"""
import asyncio
import logging
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


def enqueue(content: str, filters: dict, metadata: Optional[dict] = None) -> bool:
    """写入失败时调用，将任务加入补偿队列。返回是否成功入队。"""
    with _queue_lock:
        if len(_queue) >= _queue.maxlen:
            logger.warning("补偿队列已满(%d)，拒绝新任务", len(_queue))
            return False
        _queue.append({
            "content": content,
            "filters": filters,
            "metadata": metadata,
            "retries": 0,
            "next_retry_at": time.time() + BASE_DELAY,
            "created_at": time.time(),
        })
        logger.info("补偿队列: +1, 当前深度=%d", len(_queue))
        return True


async def _worker(write_fn: Callable):
    """后台重试 worker，指数退避。"""
    while not _stop_event.is_set():
        task = None
        with _queue_lock:
            if _queue and _queue[0]["next_retry_at"] <= time.time():
                task = _queue[0]

        if task:
            try:
                result = await write_fn(task["content"], task["filters"], task.get("metadata"))
                if result and result.get("action") != "error":
                    with _queue_lock:
                        _queue.popleft()
                    logger.info("补偿队列: 重试成功, 剩余=%d", len(_queue))
                else:
                    _bump_retry(task)
            except Exception as e:
                _bump_retry(task)
                logger.debug("补偿队列: 重试失败: %s", e)
        else:
            await asyncio.sleep(1)


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
    """启动补偿队列 worker（在 lifespan 中调用）。"""
    global _worker_task
    _stop_event.clear()

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
