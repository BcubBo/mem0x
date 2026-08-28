"""auto_expire — 自动过期清理模块（v2）

直接用 Qdrant scroll + filter 扫描，零 embedding 调用。
按 user_id 过滤 → 按 created_at 排序 → 检查 lane TTL / expires 标记 → 删除过期记忆。

旧版用 memory.search(query="记忆") 分页扫描，每次 search 触发 embedding + rerank，
11000+ 条记忆产生 3000+ 次 API 调用，烧光半个月14B模型额度。已废弃。
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .index_sync import sync_after_delete

logger = logging.getLogger("mem0x.auto_expire")

# 默认扫描间隔（秒）
DEFAULT_INTERVAL = 3600  # 1小时
# 每批扫描数量（Qdrant scroll batch）
BATCH_SIZE = 200
# 最大扫描轮数（安全阀，防止无限循环）
MAX_SCROLL_ROUNDS = 200

# 自适应衰减：retrievability 低于此阈值视为过期
DEFAULT_RETRIEVABILITY_THRESHOLD = 0.2

# lane → TTL 天数（None = 永不衰减）
_LANE_TTL = {
    "identity": None,
    "preference": None,
    "project": 180,
    "emotion": 5,
    "default": 30,
}


def _load_auto_expire_config() -> dict:
    """读取 auto_expire 配置段（模块级缓存）。"""
    try:
        from wrapper.mem0_runtime import load_config
        cfg = load_config()
        return cfg.get("auto_expire", {})
    except Exception:
        return {}

_ae_cfg = _load_auto_expire_config()

_EXPIRES_RE = re.compile(r"\[expires:(\d{4}-\d{2}-\d{2})\]")
_LANE_RE = re.compile(r"\[lane:(\w+)\]")

# 全局状态
_running = False
_thread: Optional[threading.Thread] = None


def _is_expired(data: str, created_at: Optional[str],
                 metadata: Optional[dict] = None) -> bool:
    """判断单条记忆是否过期。

    - 显式 [expires:YYYY-MM-DD] 标记：始终生效。
    - adaptive 模式且有 fsrs_card：用 retrievability < 阈值 判断（替代固定 TTL）。
    - 其他情况：保留原有 lane TTL 作为 fallback。
    """
    # 1. 显式 expires 标记（始终生效，不受 adaptive 影响）
    m = _EXPIRES_RE.search(data)
    if m:
        try:
            exp = datetime.fromisoformat(m.group(1))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) > exp
        except ValueError:
            pass

    # 2. adaptive 模式：有 fsrs_card 时用 retrievability 判断
    adaptive = _ae_cfg.get("adaptive", False)
    if adaptive and metadata and metadata.get("fsrs_card"):
        try:
            from wrapper.fsrs_bridge import compute_retrievability
            threshold = _ae_cfg.get("retrievability_threshold",
                                    DEFAULT_RETRIEVABILITY_THRESHOLD)
            R = compute_retrievability(metadata, created_at)
            if R is not None:
                return R < threshold
        except Exception as e:
            logger.debug("adaptive retrievability 计算失败，回退 TTL: %s", e)
            # fall through to lane TTL

    # 3. lane TTL（fallback / 非 adaptive 模式）
    lm = _LANE_RE.search(data)
    if lm and created_at:
        ttl_days = _LANE_TTL.get(lm.group(1))
        if ttl_days is None:
            return False  # 永不衰减
        try:
            created = datetime.fromisoformat(created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) > created + timedelta(days=ttl_days)
        except ValueError:
            pass

    return False


def _get_qdrant_client():
    """从 mem0 实例获取 Qdrant 客户端。"""
    from wrapper.mem0_runtime import get_memory
    mem = get_memory()
    if mem is None:
        return None, None
    client = mem.vector_store.client
    collection = getattr(mem.vector_store, "collection_name", "mem0")
    return client, collection


def run_expire_cycle(user_id: str = "bo") -> int:
    """执行一轮过期清理。

    直接用 Qdrant scroll + filter，零 embedding 调用。

    Returns:
        删除数量
    """
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    client, collection = _get_qdrant_client()
    if client is None:
        logger.warning("auto_expire: Qdrant 客户端不可用")
        return 0
    logger.debug("auto_expire: 开始扫描 user_id=%s", user_id)

    # 只扫描指定用户的记忆
    # TODO: 加 created_at Range 预过滤需要写入链路存数字时间戳
    user_filter = Filter(
        must=[
            FieldCondition(key="user_id", match=MatchValue(value=user_id)),
        ]
    )

    deleted = 0
    scanned = 0
    offset = None

    try:
        for _round in range(MAX_SCROLL_ROUNDS):
            result = client.scroll(
                collection_name=collection,
                scroll_filter=user_filter,
                limit=BATCH_SIZE,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            points, next_offset = result
            if not points:
                break

            for point in points:
                scanned += 1
                data = point.payload.get("data", "")
                created_at = point.payload.get("created_at")
                metadata = point.payload.get("metadata")

                if not data or not _is_expired(data, created_at, metadata):
                    continue

                # 跳过核心记忆
                try:
                    from wrapper.core_memory import is_core_memory
                    if is_core_memory(point.id):
                        continue
                except ImportError:
                    pass

                # 跳过已归档/已删除的记忆（其他系统已处理）
                payload = point.payload or {}
                if payload.get("archived") or payload.get("deleted_at"):
                    continue

                try:
                    # 软删除：设置 deleted_at 标记而非硬删 Qdrant 点
                    from datetime import datetime, timezone
                    client.set_payload(
                        collection_name=collection,
                        payload={"deleted_at": datetime.now(timezone.utc).isoformat()},
                        points=[point.id],
                    )
                    deleted += 1
                    sync_after_delete(str(point.id), user_id)
                    logger.info("已标记过期记忆软删除: %s | %.40s", point.id, data)
                except Exception as e:
                    logger.warning("标记软删除失败 %s: %s", point.id, e)


            if next_offset is None:
                break
            offset = next_offset

    except Exception as e:
        logger.error("auto_expire 扫描异常 (已扫描 %d, 已删除 %d): %s", scanned, deleted, e)

    logger.info("auto_expire 完成: 扫描 %d 条, 删除 %d 条", scanned, deleted)
    return deleted


def _background_loop(interval: int = DEFAULT_INTERVAL):
    """后台循环线程（按 user_id 分批处理所有用户）。"""
    global _running
    logger.info("auto_expire 后台线程启动，间隔 %ds", interval)

    from wrapper.evolve_lock import background_tasks_lock

    while _running:
        try:
            # 获取共享锁
            if not background_tasks_lock.acquire(timeout=5):
                logger.debug("auto_expire: 等待共享锁超时，跳过本轮")
                time.sleep(interval)
                continue
            try:
                # 获取所有 user_id
                from wrapper.mem0_runtime import get_memory
                from wrapper.fetch_all import get_distinct_user_ids
                memory = get_memory()
                user_ids = asyncio.run(get_distinct_user_ids(memory))
            except Exception:
                user_ids = ["bo"]  # fallback

            total_deleted = 0
            for uid in user_ids:
                deleted = run_expire_cycle(user_id=uid)
                total_deleted += deleted
            if total_deleted > 0:
                logger.info("本轮清理 %d 条过期记忆（%d 个用户）", total_deleted, len(user_ids))
        except Exception as e:
            logger.error("auto_expire 循环异常: %s", e)
        finally:
            try:
                background_tasks_lock.release()
            except Exception:
                pass

        time.sleep(interval)

    logger.info("auto_expire 后台线程已停止")


def start(memory_getter=None, interval: int = DEFAULT_INTERVAL):
    """启动后台清理线程。memory_getter 保留参数兼容旧调用，v2 内部自行获取 Qdrant。"""
    global _running, _thread
    if _running:
        logger.warning("auto_expire 已在运行")
        return

    _running = True
    _thread = threading.Thread(
        target=_background_loop,
        args=(interval,),
        daemon=True,
        name="auto-expire",
    )
    _thread.start()


def stop():
    """停止后台清理线程。"""
    global _running
    _running = False


def is_running() -> bool:
    return _running
