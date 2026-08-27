"""reflect — 反思引擎模块

定期分析记忆系统的整体表现，生成改进建议：
- 识别记忆断层（缺失的关键信息）
- 发现矛盾记忆
- 评估搜索质量
- 生成优化建议
"""
from __future__ import annotations
import asyncio

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict, List

logger = logging.getLogger("mem0x.reflect")

# 后台扫描间隔（秒）
DEFAULT_INTERVAL = 21600  # 6小时

# 全局状态
_running = False
_thread: Optional[threading.Thread] = None
_db_path: Optional[str] = None
_lock = threading.Lock()
_health_lock = threading.Lock()


def _get_db_path() -> str:
    global _db_path
    if _db_path is None:
        from security.utils import get_data_dir
        _db_path = os.path.join(get_data_dir(), "reflect.db")
    return _db_path


def _init_db():
    """初始化 reflect SQLite 表。"""
    db_path = _get_db_path()
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reflect_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_at TEXT DEFAULT (datetime('now')),
                    total_memories INTEGER,
                    quality_score REAL,
                    issues TEXT,
                    suggestions TEXT
                )
            """)
            conn.commit()
    except Exception as e:
        logger.error("reflect DB 初始化失败: %s", e)


def _ensure_db():
    with _lock:
        _init_db()


async def analyze_system_health(memory, user_id: str = "bo", agent_id: str = "hermes") -> Dict:
    """分析系统健康状态。"""
    if not _health_lock.acquire(blocking=False):
        return {"total_memories": 0, "quality_score": 0.0, "issues": ["分析正在进行中"], "suggestions": []}

    try:
        return await _analyze_system_health_inner(memory, user_id, agent_id)
    finally:
        _health_lock.release()


async def _analyze_system_health_inner(memory, user_id: str, agent_id: str) -> Dict:
    """分析系统健康状态（内部实现，调用方负责加锁）。"""
    health = {
        "total_memories": 0,
        "quality_score": 0.0,
        "issues": [],
        "suggestions": [],
    }

    try:
        filters = {"user_id": user_id}
        if agent_id:
            filters["agent_id"] = agent_id

        # 使用 Qdrant count API 获取精确总数
        try:
            from wrapper.mem0_runtime import get_memory
            mem = get_memory()
            if mem:
                from qdrant_client.models import Filter as QFilter, FieldCondition, MatchValue
                qc = mem.vector_store.client
                collection = getattr(mem, "collection_name", "mem0")
                count_result = qc.count(
                    collection_name=collection,
                    count_filter=QFilter(must=[
                        FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                    ]),
                    exact=True,
                )
                health["total_memories"] = count_result.count
        except Exception as e:
            logger.debug("Qdrant count 失败，降级到 search: %s", e)
            # 降级：用 search 估算
            results = await memory.search(query="记忆", filters=filters, top_k=500)
            items = results.get("results", []) if isinstance(results, dict) else []
            health["total_memories"] = len(items)

        # 使用占位符查询获取记忆用于质量分析
        results = await memory.search(query="记忆", filters=filters, top_k=500)
        items = results.get("results", []) if isinstance(results, dict) else []

        if not items:
            health["issues"].append("记忆库为空")
            health["suggestions"].append("开始记录对话以积累记忆")
            return health

        # 质量评分
        scores = [item.get("score", 0) or 0 for item in items]
        avg_score = sum(scores) / len(scores) if scores else 0
        health["quality_score"] = round(avg_score, 3)

        # 检查问题
        low_score_count = sum(1 for s in scores if s < 0.3)
        if low_score_count > len(scores) * 0.3:
            health["issues"].append(f"低质量记忆占比过高: {low_score_count}/{len(scores)}")
            health["suggestions"].append("运行 /evolve 清理低质量记忆")

        # 检查是否有过多重复
        texts = [item.get("memory", "")[:100] for item in items[:50]]
        unique_ratio = len(set(texts)) / len(texts) if texts else 1
        if unique_ratio < 0.7:
            health["issues"].append(f"记忆重复率过高: {1-unique_ratio:.1%}")
            health["suggestions"].append("运行 /consolidate 合并重复记忆")

        # 检查时间分布
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        recent_count = 0
        for item in items:
            created_at = item.get("created_at")
            if created_at:
                try:
                    created = datetime.fromisoformat(created_at)
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if (now - created).days <= 7:
                        recent_count += 1
                except Exception:
                    pass

        if recent_count < 5:
            health["suggestions"].append("近期记忆较少，建议增加对话频率")

        if not health["issues"]:
            health["issues"].append("系统运行正常")

    except Exception as e:
        logger.error("分析系统健康失败: %s", e)
        health["issues"].append(f"分析失败: {e}")

    return health


async def run_reflect_cycle(memory, user_id: str = "bo", agent_id: str = "hermes") -> Dict:
    """执行一轮反思，返回分析结果。"""
    result = {"status": "ok", "health": {}}

    try:
        health = await analyze_system_health(memory, user_id, agent_id)
        result["health"] = health

        # 记录到 SQLite
        _ensure_db()
        try:
            with sqlite3.connect(_get_db_path()) as conn:
                conn.execute("""
                    INSERT INTO reflect_logs
                    (total_memories, quality_score, issues, suggestions)
                    VALUES (?, ?, ?, ?)
                """, (
                    health["total_memories"],
                    health["quality_score"],
                    json.dumps(health["issues"], ensure_ascii=False),
                    json.dumps(health["suggestions"], ensure_ascii=False),
                ))
                conn.commit()
        except Exception as e:
            logger.debug("记录反思日志失败: %s", e)

        # 过滤掉"系统运行正常"，只统计真正的问题
        real_issues = [i for i in health["issues"] if i != "系统运行正常"]
        logger.info("反思完成: 质量%.2f, 问题%d条, 建议%d条",
                    health["quality_score"],
                    len(real_issues),
                    len(health["suggestions"]))
        for issue in real_issues:
            logger.info("⚠️ reflect.issue: %s", issue)

    except Exception as e:
        logger.error("反思失败: %s", e)
        result["status"] = "error"
        result["error"] = str(e)

    return result


def list_reflect_logs(limit: int = 10) -> List[Dict]:
    """列出最近的反思日志。"""
    _ensure_db()
    try:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM reflect_logs ORDER BY id DESC LIMIT ?", (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error("列出反思日志失败: %s", e)
        return []


def _background_loop(memory_getter, interval: int = DEFAULT_INTERVAL):
    """后台循环线程。"""
    global _running
    logger.info("reflect 后台线程启动，间隔 %ds", interval)

    while _running:
        try:
            memory = memory_getter()
            if memory:
                result = asyncio.run(run_reflect_cycle(memory))
                # 过滤掉"系统运行正常"，只统计真正的问题
                issues = result.get("health", {}).get("issues", [])
                real_issues = [i for i in issues if i != "系统运行正常"]
                if real_issues:
                    logger.info("反思发现 %d 个问题", len(real_issues))
        except Exception as e:
            logger.error("reflect 循环异常: %s", e)

        time.sleep(interval)

    logger.info("reflect 后台线程已停止")


def start(memory_getter, interval: int = DEFAULT_INTERVAL):
    """启动后台反思线程。"""
    global _running, _thread
    if _running:
        logger.warning("reflect 已在运行")
        return

    _running = True
    _thread = threading.Thread(
        target=_background_loop,
        args=(memory_getter, interval),
        daemon=True,
        name="reflect",
    )
    _thread.start()


def stop():
    """停止后台反思线程。"""
    global _running
    _running = False


def is_running() -> bool:
    return _running


# 初始化
_ensure_db()
