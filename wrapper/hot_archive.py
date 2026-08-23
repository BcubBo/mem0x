"""hot_archive — 热知识自动归档

从 salience.db 找高频访问 + 高 salience 的记忆，自动升级为核心记忆。
后台线程定期执行 + 手动触发。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("mem0x.hot_archive")

# ── 后台线程控制 ──
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_running = False

# ── 默认配置 ──
DEFAULT_INTERVAL = 3600 * 6      # 6 小时执行一次
DEFAULT_SALIENCE_THRESHOLD = 0.8  # salience >= 0.8 才归档
DEFAULT_ACCESS_THRESHOLD = 5      # access_count >= 5 才归档
DEFAULT_CATEGORY = "hot_archive"  # 核心记忆分类


def _get_config() -> dict:
    """从 config.json 读取 hot_archive 配置。"""
    try:
        from security.utils import get_config
        return get_config().get("hot_archive", {})
    except Exception:
        return {}


def find_hot_candidates() -> List[Dict[str, Any]]:
    """从 salience.db 查询热知识候选。

    条件：salience >= threshold 且 access_count >= access_threshold
    排除：已经是 core_memory 的记忆
    """
    import os
    import sqlite3
    from wrapper import core_memory

    cfg = _get_config()
    sal_threshold = cfg.get("salience_threshold", DEFAULT_SALIENCE_THRESHOLD)
    acc_threshold = cfg.get("access_threshold", DEFAULT_ACCESS_THRESHOLD)

    # 获取 salience.db 路径
    try:
        from security.utils import get_data_dir
        db_path = os.path.join(get_data_dir(), "salience.db")
    except Exception:
        logger.debug("hot_archive: get_data_dir 失败")
        return []

    if not os.path.exists(db_path):
        return []

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row

        # 查找高 salience + 高访问次数的记忆
        rows = conn.execute(
            "SELECT memory_id, salience, access_count, content_preview "
            "FROM salience "
            "WHERE salience >= ? AND access_count >= ? "
            "ORDER BY salience * access_count DESC "
            "LIMIT 50",
            (sal_threshold, acc_threshold),
        ).fetchall()
        conn.close()

        # 排除已经是核心记忆的
        candidates = []
        for row in rows:
            mid = row["memory_id"]
            if not core_memory.is_core_memory(mid):
                candidates.append({
                    "memory_id": mid,
                    "salience": row["salience"],
                    "access_count": row["access_count"],
                    "content_preview": row["content_preview"] or "",
                })

        return candidates

    except Exception as e:
        logger.debug("hot_archive: 查询失败: %s", e)
        return []


def archive_hot_memories(candidates: Optional[List[Dict]] = None) -> Dict[str, Any]:
    """将热知识候选归档为核心记忆。

    返回：{archived: int, skipped: int, errors: int, details: [...]}
    """
    from wrapper import core_memory

    cfg = _get_config()
    category = cfg.get("category", DEFAULT_CATEGORY)

    if candidates is None:
        candidates = find_hot_candidates()

    archived = 0
    skipped = 0
    errors = 0
    details = []

    for c in candidates:
        mid = c["memory_id"]
        content = c.get("content_preview", "")
        salience = c.get("salience", 0)

        # 根据 salience 计算 importance（线性映射到 0.5-1.0）
        importance = min(1.0, 0.5 + salience * 0.5)

        try:
            ok = core_memory.add_core_memory(
                mid, content, category=category, importance=importance,
            )
            if ok:
                archived += 1
                details.append({
                    "memory_id": mid,
                    "salience": salience,
                    "access_count": c.get("access_count", 0),
                    "importance": importance,
                })
                logger.info("hot_archive: 归档 %s (sal=%.2f, imp=%.2f)", mid[:8], salience, importance)
            else:
                skipped += 1
        except Exception as e:
            errors += 1
            logger.debug("hot_archive: 归档失败 %s: %s", mid[:8], e)

    return {
        "archived": archived,
        "skipped": skipped,
        "errors": errors,
        "total_candidates": len(candidates),
        "details": details,
    }


def run_archive_cycle() -> Dict[str, Any]:
    """完整归档周期：查找候选 → 归档 → 返回结果。"""
    start = time.time()
    candidates = find_hot_candidates()
    if not candidates:
        return {"archived": 0, "skipped": 0, "errors": 0, "total_candidates": 0, "elapsed_ms": 0}
    result = archive_hot_memories(candidates)
    result["elapsed_ms"] = int((time.time() - start) * 1000)
    return result


# ── 后台线程 ──

def _loop():
    """后台循环：定期执行归档。"""
    global _running
    _running = True
    logger.info("hot_archive 后台线程已启动")

    while not _stop_event.is_set():
        try:
            cfg = _get_config()
            interval = cfg.get("interval", DEFAULT_INTERVAL)

            result = run_archive_cycle()
            if result["archived"] > 0:
                logger.info("hot_archive: 归档 %d 条热知识", result["archived"])

            # 等待下一轮（可中断）
            _stop_event.wait(timeout=interval)
        except Exception as e:
            logger.debug("hot_archive 循环异常: %s", e)
            _stop_event.wait(timeout=60)

    _running = False
    logger.info("hot_archive 后台线程已停止")


def start(get_memory_fn=None) -> None:
    """启动后台线程。"""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="hot_archive", daemon=True)
    _thread.start()


def stop() -> None:
    """停止后台线程。"""
    _stop_event.set()
    if _thread:
        _thread.join(timeout=5)


def is_running() -> bool:
    """后台线程是否在运行。"""
    return _running and _thread is not None and _thread.is_alive()
