"""全量记忆获取：绕过 mem0 的 get_all 限制，支持游标分页。

mem0 的 get_all(top_k=N) 最多返回 N 条，不支持 offset。
此模块直接调用 Qdrant scroll API，用 PointId 做游标分页。

提供两种模式：
- fetch_all_memories(): 一次性获取（兼容旧接口）
- iter_batches(): 生成器模式，每次 yield 一批（推荐，不占用大量内存）
"""
import logging
from typing import List, Dict, Any, Optional, AsyncGenerator

logger = logging.getLogger("mem0x.fetch_all")


def _build_filter(filters: dict = None):
    """构造 Qdrant 过滤器。"""
    if not filters:
        return None
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    conditions = []
    for key, value in filters.items():
        if value:
            conditions.append(FieldCondition(key=key, match=MatchValue(value=value)))
    return Filter(must=conditions) if conditions else None


def _point_to_item(point) -> dict:
    """将 Qdrant Point 转换为记忆 dict。"""
    return {
        "id": str(point.id),
        "memory": point.payload.get("data", ""),
        "metadata": {k: v for k, v in point.payload.items()
                     if k not in ("data", "hash", "text_lemmatized")},
        "created_at": point.payload.get("created_at"),
        "updated_at": point.payload.get("updated_at"),
    }


async def iter_batches(memory_instance, filters: dict = None,
                       batch_size: int = 200,
                       max_items: int = 0) -> AsyncGenerator[List[dict], None]:
    """分批获取全量记忆（生成器模式，不一次性加载到内存）。

    Args:
        memory_instance: mem0 AsyncMemory 实例
        filters: 过滤条件
        batch_size: 每批大小
        max_items: 最大获取条数（0=不限制）

    Yields:
        每批记忆列表
    """
    try:
        client = memory_instance.vector_store.client
        collection = memory_instance.vector_store.collection_name
    except AttributeError:
        logger.warning("无法获取 Qdrant client，fallback 到 get_all")
        result = await memory_instance.get_all(filters=filters, top_k=max_items or 500)
        items = result.get("results", []) if isinstance(result, dict) else []
        if items:
            yield items
        return

    query_filter = _build_filter(filters)
    offset = None
    total = 0

    while True:
        if max_items > 0 and total >= max_items:
            break

        try:
            limit = min(batch_size, max_items - total) if max_items > 0 else batch_size
            result = client.scroll(
                collection_name=collection,
                scroll_filter=query_filter,
                limit=limit,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            logger.warning("Qdrant scroll 失败: %s", e)
            break

        points, next_offset = result
        if not points:
            break

        batch = [_point_to_item(p) for p in points]
        total += len(batch)
        yield batch

        if next_offset is None or next_offset == offset:
            break
        offset = next_offset

    logger.info("iter_batches: 共获取 %d 条记忆", total)


async def fetch_all_memories(memory_instance, filters: dict = None,
                            max_items: int = 2000) -> List[dict]:
    """一次性获取全量记忆（兼容旧接口，内部使用分批）。"""
    all_items = []
    async for batch in iter_batches(memory_instance, filters, batch_size=500, max_items=max_items):
        all_items.extend(batch)
    return all_items
