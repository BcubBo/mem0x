"""tags_hook.py — spaCy NER tags 统一管理 hook。

add/update 时提取实体写入 Qdrant payload tags，
delete 时清理 tags。
"""

import logging
from typing import Optional

logger = logging.getLogger("tags_hook")


def _extract_and_setTags(memory, memory_id: str, content: str) -> None:
    """从内容提取 tags 并写入 Qdrant payload。"""
    try:
        from wrapper.spacy_ner import extract_tags
        tags = extract_tags(content)
        qc = memory.vector_store.client
        collection = getattr(memory, "collection_name", "mem0")
        if tags:
            qc.set_payload(collection, payload={"tags": tags}, points=[memory_id])
            logger.debug("tags_hook: id=%s tags=%s", memory_id[:12], tags)
        else:
            qc.set_payload(collection, payload={"tags": []}, points=[memory_id])
    except Exception as e:
        logger.debug("tags_hook set 失败: %s", e)


def _clear_tags(memory, memory_id: str) -> None:
    """清空 tags（软删除时调用）。"""
    try:
        qc = memory.vector_store.client
        collection = getattr(memory, "collection_name", "mem0")
        qc.set_payload(collection, payload={"tags": []}, points=[memory_id])
    except Exception as e:
        logger.debug("tags_hook clear 失败: %s", e)


def on_add(memory, memory_id: str, content: str) -> None:
    """add 后提取 tags。"""
    _extract_and_setTags(memory, memory_id, content)


def on_update(memory, memory_id: str, content: str) -> None:
    """update 后重新提取 tags。"""
    _extract_and_setTags(memory, memory_id, content)


def on_delete(memory, memory_id: str) -> None:
    """软删除时清空 tags（硬删时 Qdrant 自动清除）。"""
    _clear_tags(memory, memory_id)
