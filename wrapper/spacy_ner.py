"""spaCy NER 实体提取器 — 提取人名/组织/产品等实体，存入 Qdrant payload tags。"""

import logging
from collections import Counter
from typing import Any

logger = logging.getLogger("spacy_ner")

# 停用实体
_STOP_ENTITIES = frozenset({
    "the", "a", "an", "is", "are", "was", "的", "了", "在", "是",
    "和", "有", "为", "这", "中", "不", "也", "用户", "助手",
    "memory", "data", "config", "system", "test", "tool",
})

_nlp = None


def _get_nlp():
    global _nlp
    if _nlp is None:
        import spacy
        # 优先加载 Transformer 模型（NER F1=74%），回退到 sm（~50%）
        for model in ["zh_core_web_trf", "zh_core_web_sm", "en_core_web_sm"]:
            try:
                _nlp = spacy.load(model)
                logger.info("spaCy 模型加载成功: %s", model)
                break
            except Exception:
                continue
        else:
            logger.warning("所有 spaCy 模型加载失败")
            _nlp = False
    return _nlp if _nlp is not False else None


def extract_tags(content: str, top_n: int = 10) -> list[str]:
    """从文本中提取实体标签，去重+按频率排序。"""
    nlp = _get_nlp()
    if nlp is None:
        return []
    try:
        doc = nlp(content[:10000])
        entities = [
            ent.text.strip() for ent in doc.ents
            if len(ent.text.strip()) >= 2 and ent.text.strip().lower() not in _STOP_ENTITIES
        ]
        counter = Counter(entities)
        return [word for word, _ in counter.most_common(top_n)]
    except Exception as e:
        logger.debug("spaCy NER 失败: %s", e)
        return []


def extract_tags_with_types(content: str, top_n: int = 10) -> list[dict[str, str]]:
    """提取实体并返回 text + label（供训练缓冲区使用）。"""
    nlp = _get_nlp()
    if nlp is None:
        return []
    try:
        doc = nlp(content[:10000])
        seen = set()
        results = []
        for ent in doc.ents:
            t = ent.text.strip()
            if len(t) < 2 or t.lower() in _STOP_ENTITIES:
                continue
            if t not in seen:
                seen.add(t)
                results.append({"text": t, "label": ent.label_})
        return results[:top_n]
    except Exception as e:
        logger.debug("spaCy NER (with types) 失败: %s", e)
        return []


def is_available() -> bool:
    return _get_nlp() is not None
