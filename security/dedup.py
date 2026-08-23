"""写入去重 — mem0_add 前检查重复，Jaccard >0.85 则更新而非新增。

数字值变化（如端口）视为不同内容，不触发去重。
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger("mem0x.dedup")

# Bigram 去停用词
_STOP_WORDS = frozenset({
    "的", "是", "在", "了", "和", "有", "我", "他", "她", "它",
    "这", "那", "都", "就", "也", "把", "被", "让", "给", "用",
    "从", "到", "对", "为", "与", "或", "但", "而", "又", "还",
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "can", "could", "should", "may", "might", "to", "of", "in",
    "for", "on", "with", "at", "by", "from", "as", "into", "that",
    "which", "this", "these", "those", "it", "its",
})

# 数字变化检测正则
_NUM_CHANGE_PATTERN = re.compile(r'\d{4,6}')


def _has_numeric_change(text_a: str, text_b: str) -> bool:
    """检测两个文本中的数字值是否发生变化。"""
    nums_a = set(_NUM_CHANGE_PATTERN.findall(text_a))
    nums_b = set(_NUM_CHANGE_PATTERN.findall(text_b))
    # 如果数字集合不同，说明有数字变化
    return nums_a != nums_b


def _tokenize(text: str) -> set:
    """Bigram + 去停用词。"""
    clean = re.sub(r"\[lane:\w+\]", "", text.lower().strip())  # 去掉 lane 标签
    clean = re.sub(r"\[expires:\d{4}-\d{2}-\d{2}\]", "", clean)  # 去掉过期标记
    clean = re.sub(r"\s+", " ", clean)
    bigrams = {clean[i:i+2] for i in range(len(clean) - 1)}
    words = set(clean.split()) - _STOP_WORDS
    return bigrams | words


def jaccard_sim(a: str, b: str) -> float:
    """Bigram 级 Jaccard 相似度。"""
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_duplicate(backend, new_content: str, filters: dict, *, threshold: float = 0.85,
                   _pre_results: list = None) -> Optional[Tuple[str, str, float]]:
    """搜索是否有重复记忆。返回 (memory_id, existing_text, similarity) 或 None。

    数字值变化（如端口）视为不同内容，不触发去重。

    _pre_results: 由 pipeline 传入的共享搜索结果，避免重复 embedder 调用。
    """
    try:
        if _pre_results is not None:
            results = _pre_results
        else:
            results = backend.search(new_content, filters=filters, top_k=5)
        if not results:
            return None
        for r in results:
            existing_text = r.get("memory", "") or r.get("text", "")
            existing_id = r.get("id", "")
            meta = r.get("metadata") or {}
            if not existing_text or not existing_id:
                continue
            
            # 跳过已归档的记忆
            if meta.get("archived"):
                continue
            
            # 数字值变化不视为重复
            if _has_numeric_change(new_content, existing_text):
                logger.debug("Dedup skip: numeric change detected between '%s' and '%s'",
                           new_content[:30], existing_text[:30])
                continue
            
            sim = jaccard_sim(new_content, existing_text)
            if sim >= threshold:
                logger.info("🔍 Dedup hit: sim=%.3f, id=%s, preview='%s'", sim, existing_id[:8], existing_text[:50])
                return (existing_id, existing_text, sim)
    except Exception as e:
        logger.debug("Dedup search failed: %s", e)
    return None
