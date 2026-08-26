"""全量记忆获取：绕过 mem0 的 get_all 限制，支持游标分页。

mem0 的 get_all(top_k=N) 最多返回 N 条，不支持 offset。
此模块直接调用 Qdrant scroll API，用 PointId 做游标分页。
"""
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("mem0x.fetch_all")


async def fetch_all_memories(memory_instance, filters: dict = None, max_items: int = 2000) -> List[dict]:
    """分页获取全量记忆（游标分页，绕过 mem0 get_all 限制）。

    Args:
        memory_instance: mem0 AsyncMemory 实例
        filters: 过滤条件（如 {"user_id": "bo"}）
        max_items: 最大获取条数

    Returns:
        记忆列表 [{"id": ..., "memory": ..., "metadata": ..., ...}, ...]
    """
    try:
        client = memory_instance.vector_store.client
        collection = memory_instance.vector_store.collection_name
    except AttributeError:
        # fallback 到 mem0 get_all
        logger.warning("无法获取 Qdrant client，fallback 到 get_all")
        result = await memory_instance.get_all(filters=filters, top_k=max_items)
        return result.get("results", []) if isinstance(result, dict) else []

    # 构造 Qdrant 过滤器
    query_filter = None
    if filters:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        conditions = []
        for key, value in filters.items():
            if value:
                conditions.append(FieldCondition(key=key, match=MatchValue(value=value)))
        if conditions:
            query_filter = Filter(must=conditions)

    # 游标分页
    all_items = []
    offset = None
    page_size = min(max_items, 500)

    while len(all_items) < max_items:
        try:
            result = client.scroll(
                collection_name=collection,
                scroll_filter=query_filter,
                limit=page_size,
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

        for point in points:
            item = {
                "id": str(point.id),
                "memory": point.payload.get("data", ""),
                "metadata": {k: v for k, v in point.payload.items()
                             if k not in ("data", "hash", "text_lemmatized")},
                "created_at": point.payload.get("created_at"),
                "updated_at": point.payload.get("updated_at"),
            }
            all_items.append(item)

        # 游标分页：offset 是下一批的起始 PointId
        if next_offset is None or next_offset == offset:
            break
        offset = next_offset

    logger.info("fetch_all_memories: 获取 %d 条记忆", len(all_items))
    return all_items[:max_items]
