"""evolve_mem — 记忆自进化模块

定期分析记忆质量，自动优化：
- 合并碎片信息
- 提升重要记忆的权重
- 降级低质量记忆
- 生成记忆摘要
"""
from __future__ import annotations
import asyncio

import logging
import threading
import time
from typing import Optional, Dict, List

logger = logging.getLogger("mem0x.evolve_mem")

# ── 配置（从 config-compose.json 读取）──────────────
def _load_config():
    try:
        from wrapper.mem0_runtime import load_config
        config = load_config()
        return config.get("evolve_mem", {})
    except Exception:
        return {}

_cfg = _load_config()

# 后台扫描间隔（秒）
DEFAULT_INTERVAL = _cfg.get("interval", 14400)  # 默认4小时

# 质量阈值
THRESHOLD_HIGH = _cfg.get("threshold_high", 0.7)
THRESHOLD_LOW = _cfg.get("threshold_low", 0.3)

# 全局状态
_running = False
_thread: Optional[threading.Thread] = None


async def analyze_memory_quality(memory, user_id: str = "bo", agent_id: str = "hermes") -> Dict:
    """分析记忆质量，返回统计信息。

    基于 FSRS（Free Spaced Repetition Scheduler）理论 + 文本特征：
    ============================================================
    FSRS 遗忘曲线：R(t, S) = (1 + factor * t/S)^(-1)
    - R: Retrievability（可回忆性）
    - S: Stability（稳定性）
    - t: elapsed_days（经过天数）
    - factor: 0.9（90% 记忆保持率）

    稳定性计算：S = (1 + search_count * 0.5 + update_count * 0.3) * (1 + age_days/30)
    - search_count: 被搜索次数（越多越稳定）
    - update_count: 被更新次数（越多越稳定）
    - age_days: 创建天数（记忆固化）

    文本信息密度：D = entity_density + keyword_density
    - entity_density: 实体密度（数字、英文、专有名词）
    - keyword_density: 关键词密度（技术术语）

    质量分数：使用标准 FSRS-6 算法（wrapper/fsrs_bridge.py）
    ============================================================
    """
    import re
    from datetime import datetime, timezone
    from wrapper.fsrs_bridge import compute_retrievability, get_quality_score, card_from_metadata

    def text_density(text: str) -> float:
        """文本信息密度：实体密度 + 关键词密度"""
        # 实体密度（数字、英文、中文词组）
        entities = re.findall(r'[A-Z][a-z]+|\d+|[\u4e00-\u9fff]{2,}', text)
        entity_density = min(len(entities) / max(len(text), 1) * 10, 1.0)
        
        # 关键词密度（技术术语）
        keywords = ['mem0', 'Qdrant', 'Neo4j', 'Docker', 'API', 'plugin', 'config',
                    '插件', '配置', '测试', '部署', '修复', '优化']
        keyword_count = sum(1 for kw in keywords if kw.lower() in text.lower())
        keyword_density = min(keyword_count / 5, 1.0)
        
        return min(entity_density * 0.6 + keyword_density * 0.4, 1.0)

    # 阈值配置
    # 使用模块级配置阈值

    stats = {
        "total": 0,
        "high_quality": 0,
        "medium_quality": 0,
        "low_quality": 0,
        "stale": 0,
        "by_lane": {},
        "quality_scores": [],
        "threshold_high": THRESHOLD_HIGH,
        "threshold_low": THRESHOLD_LOW,
    }

    try:
        filters = {}
        if user_id:
            filters["user_id"] = user_id
        if agent_id:
            filters["agent_id"] = agent_id
        try:
            from wrapper.fetch_all import iter_batches
            total = 0
            async for batch in iter_batches(memory, filters=filters, batch_size=200, max_items=0):
                for item in batch:
                    text = item.get("memory", "")
                    metadata = item.get("metadata") or {}
                    created_at = item.get("created_at")

                    # FSRS 质量评估（标准 FSRS-6）
                    access_count = metadata.get("access_count", 0) if isinstance(metadata.get("access_count"), (int, float)) else 0
                    Q = get_quality_score(metadata, created_at, access_count)
                    D = text_density(text)

                    stats["quality_scores"].append(Q)

                    # 统计 lane 分布
                    lane_match = re.search(r"\[lane:(\w+)\]", text)
                    lane = lane_match.group(1) if lane_match else "none"
                    stats["by_lane"][lane] = stats["by_lane"].get(lane, 0) + 1

                    # 质量分类
                    if Q >= THRESHOLD_HIGH:
                        stats["high_quality"] += 1
                    elif Q >= THRESHOLD_LOW:
                        stats["medium_quality"] += 1
                    else:
                        stats["low_quality"] += 1

                    # 过期检测
                    if created_at:
                        try:
                            created = datetime.fromisoformat(created_at)
                            if created.tzinfo is None:
                                created = created.replace(tzinfo=timezone.utc)
                            if (datetime.now(timezone.utc) - created).days > 365:
                                stats["stale"] += 1
                        except Exception:
                            pass

                total += len(batch)
                stats["total"] = total
                logger.debug("evolve_mem: 已处理 %d 条", total)
        except asyncio.TimeoutError:
            logger.warning("evolve_mem: fetch_all 超时")
            return {"total": 0, "pruned": 0, "error": "timeout"}
        except Exception as e:
            logger.warning("evolve_mem: fetch_all 失败: %s", e)
            return {"total": 0, "pruned": 0, "error": str(e)}

        # 计算统计信息
        if stats["quality_scores"]:
            scores = stats["quality_scores"]
            stats["quality_mean"] = round(sum(scores) / len(scores), 4)
            variance = sum((s - stats["quality_mean"]) ** 2 for s in scores) / len(scores)
            stats["quality_std"] = round(variance ** 0.5, 4)

        logger.info("记忆质量分析: 总%d, 高质%d, 中质%d, 低质%d, 过期%d",
                     stats["total"], stats["high_quality"], stats["medium_quality"],
                     stats["low_quality"], stats["stale"])

    except Exception as e:
        logger.error("分析记忆质量失败: %s", e)

    return stats


async def run_evolve_cycle(memory, neo4j_hook=None, user_id: str = "bo",
                    agent_id: str = "hermes") -> Dict:
    """执行一轮自进化，返回优化结果。"""
    result = {"analyzed": 0, "optimized": 0, "pruned": 0}

    try:
        # 1. 分析质量
        stats = await analyze_memory_quality(memory, user_id, agent_id)
        result["analyzed"] = stats["total"]
        logger.info("记忆质量分析: 总%d, 高质%d, 中质%d, 低质%d, 过期%d",
                    stats["total"], stats["high_quality"],
                    stats.get("medium_quality", 0), stats["low_quality"],
                    stats["stale"])

        # 2. 清理低质量记忆（Q < threshold_low 且非核心）
        if stats["low_quality"] > 0:
            from wrapper.core_memory import is_core_memory
            from wrapper.index_sync import sync_after_delete
            filters = {"user_id": user_id}
            if agent_id:
                filters["agent_id"] = agent_id

            # 获取全量记忆（游标分页）
            try:
                from wrapper.fetch_all import iter_batches
                from wrapper.fsrs_bridge import get_quality_score as _get_quality_score
                prune_threshold = max(THRESHOLD_LOW - 0.1, 0.2)

                async for batch in iter_batches(memory, filters=filters, batch_size=200, max_items=0):
                    for item in batch:
                        metadata = item.get("metadata") or {}
                        text = item.get("memory", "")
                        created_at = item.get("created_at")
                        mem_id = item.get("id")

                        if not mem_id or mem_id.startswith("neo4j:"):
                            continue

                        # FSRS 质量评估
                        access_count = metadata.get("access_count", 0) if isinstance(metadata.get("access_count"), (int, float)) else 0
                        Q = _get_quality_score(metadata, created_at, access_count)

                        if Q < prune_threshold and not is_core_memory(mem_id):
                            try:
                                await memory.delete(mem_id)
                                sync_after_delete(mem_id, user_id)
                                result["pruned"] += 1
                                if neo4j_hook and neo4j_hook.enabled:
                                    try:
                                        neo4j_hook.cleanup(mem_id)
                                    except Exception:
                                        pass
                            except Exception as e:
                                logger.debug("清理失败 %s: %s", mem_id[:16], e)
            except asyncio.TimeoutError:
                logger.warning("evolve_mem: 清理阶段超时")
            except Exception as e:
                logger.warning("evolve_mem: 清理阶段失败: %s", e)

        # 3. 记录进化日志
        if result["pruned"] > 0:
            logger.info("自进化完成: 清理 %d 条低质量记忆", result["pruned"])

    except Exception as e:
        logger.error("自进化失败: %s", e)

    return result


def _background_loop(memory_getter, interval: int = DEFAULT_INTERVAL):
    """后台循环线程（按 user_id 分批处理所有用户）。"""
    global _running
    from wrapper.evolve_lock import background_tasks_lock
    from wrapper.fetch_all import get_distinct_user_ids
    logger.info("evolve_mem 后台线程启动，间隔 %ds", interval)

    from wrapper.neo4j_hook import get_hook
    neo4j_hook = None
    try:
        neo4j_hook = get_hook()
    except Exception:
        pass

    while _running:
        try:
            if not background_tasks_lock.acquire(timeout=5):
                logger.debug("evolve_mem: 等待共享锁超时，跳过本轮")
                time.sleep(interval)
                continue
            try:
                memory = memory_getter()
                if memory:
                    # 获取所有 user_id，逐用户处理
                    user_ids = asyncio.run(get_distinct_user_ids(memory))
                    logger.info("evolve_mem: 处理 %d 个用户", len(user_ids))
                    total_pruned = 0
                    for uid in user_ids:
                        result = asyncio.run(run_evolve_cycle(memory, neo4j_hook=neo4j_hook, user_id=uid))
                        total_pruned += result.get("pruned", 0)
                    if total_pruned > 0:
                        logger.info("本轮自进化: 清理 %d 条（%d 个用户）", total_pruned, len(user_ids))
            finally:
                background_tasks_lock.release()
        except Exception as e:
            logger.error("evolve_mem 循环异常: %s", e)

        time.sleep(interval)

    logger.info("evolve_mem 后台线程已停止")


def start(memory_getter, interval: int = DEFAULT_INTERVAL):
    """启动后台自进化线程。"""
    global _running, _thread
    if _running:
        logger.warning("evolve_mem 已在运行")
        return

    _running = True
    _thread = threading.Thread(
        target=_background_loop,
        args=(memory_getter, interval),
        daemon=True,
        name="evolve-mem",
    )
    _thread.start()


def stop():
    """停止后台自进化线程。"""
    global _running
    _running = False


def is_running() -> bool:
    return _running
