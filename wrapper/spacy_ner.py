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
        try:
            import spacy
            _nlp = spacy.load("zh_core_web_sm")
            logger.info("spaCy 中文模型加载成功")
        except Exception as e:
            logger.debug("spaCy zh model load failed: %s", e)
            try:
                import spacy
                _nlp = spacy.load("en_core_web_sm")
                logger.warning("中文模型不可用，降级到英文模型")
            except Exception as e:
                logger.warning("spaCy 模型加载失败: %s", e)
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


def is_available() -> bool:
    return _get_nlp() is not None
