"""NER 弱监督标注器 — 将 spaCy 预训练输出转化为 spaCy 训练格式。

训练管线阶段2（弱监督标注层）：
1. 用 spaCy 预训练模型的 NER 输出作为银标签（silver labels）
2. 叠加规则修正（中文人名后缀、机构后缀等）
3. 输出 spaCy Example 格式，供 nlp.update() 增量训练
"""

import logging
import re
from typing import Any

logger = logging.getLogger("ner_labeler")

# ── 规则修正层 ──

# 中文人名常见后缀（实体文本末尾带这些 → 标注为 PERSON）
_PERSON_SUFFIXES = ("老师", "先生", "女士", "博士", "教授", "总", "工")

# 中文机构后缀（实体文本末尾带这些 → 标注为 ORG）
_ORG_SUFFIXES = (
    "公司", "集团", "大学", "学院", "研究院", "研究所", "医院", "银行",
    "基金", "协会", "委员会", "局", "部", "厅", "处", "院",
    "中心", "联盟", "实验室", "基金会",
)

# 中文地名后缀（→ GPE / LOC）
_GPE_SUFFIXES = (
    "省", "市", "区", "县", "镇", "村", "州", "府", "路", "街",
    "国", "洲", "岛", "山", "河", "湖", "海", "港",
)

# 软件/产品常见模式
_PRODUCT_PATTERNS = [
    re.compile(r"[A-Z][a-zA-Z]+\d+"),          # Chrome90, GPT4
    re.compile(r"[\u4e00-\u9fff]{2,}(?:OS|AI|DB|API|SDK|Pro|Max|Plus)"),
]


def _rule_override(text: str, ent_text: str, ent_label: str) -> str:
    """基于规则修正实体标签。返回修正后的 label。"""
    t = ent_text.strip()

    # 人名后缀修正
    if t.endswith(_PERSON_SUFFIXES) and len(t) <= 8:
        return "PERSON"

    # 机构后缀修正
    if t.endswith(_ORG_SUFFIXES):
        return "ORG"

    # 地名后缀修正
    if t.endswith(_GPE_SUFFIXES) and len(t) <= 6:
        return "GPE"

    # 产品模式修正
    for pat in _PRODUCT_PATTERNS:
        if pat.search(t):
            return "PRODUCT"

    return ent_label


def _is_valid_entity(text: str, ent_text: str, label: str) -> bool:
    """过滤低质量实体。"""
    t = ent_text.strip()
    if len(t) < 2:
        return False
    # 纯标点或纯数字
    if all(not c.isalnum() for c in t):
        return False
    if t.isdigit():
        return False
    # 实体文本必须在原文中能找到
    if t not in text:
        return False
    return True


def _resolve_overlaps(entities: list[dict]) -> list[dict]:
    """移除重叠实体，保留最长的。"""
    if len(entities) <= 1:
        return entities

    # 按 start 排序，start 相同按 end 降序（长的优先）
    entities.sort(key=lambda e: (e["start"], -e["end"]))
    result = []
    for ent in entities:
        if not result:
            result.append(ent)
            continue
        prev = result[-1]
        # 有重叠 → 保留更长的
        if ent["start"] < prev["end"]:
            if (ent["end"] - ent["start"]) > (prev["end"] - prev["start"]):
                result[-1] = ent
        else:
            result.append(ent)
    return result


def label_sample(text: str, entities: list[dict[str, Any]]) -> dict | None:
    """将一条原始样本转化为弱监督标注。

    输入：
        text: 原始文本
        entities: [{"text": "张三", "label": "PERSON"}, ...]

    输出（spaCy training 格式）或 None（样本无效时）：
        {"text": "...", "entities": [(start, end, "LABEL"), ...]}
    """
    if not text or not entities:
        return None

    labeled = []
    for ent in entities:
        ent_text = ent.get("text", "").strip()
        ent_label = ent.get("label", "MISC")

        if not _is_valid_entity(text, ent_text, ent_label):
            continue

        # 规则修正
        ent_label = _rule_override(text, ent_text, ent_label)

        # 在原文中定位（取第一次出现）
        idx = text.find(ent_text)
        if idx < 0:
            continue
        labeled.append({
            "start": idx,
            "end": idx + len(ent_text),
            "label": ent_label,
        })

    if not labeled:
        return None

    labeled = _resolve_overlaps(labeled)
    return {
        "text": text,
        "entities": [(e["start"], e["end"], e["label"]) for e in labeled],
    }


def label_batch(samples: list[dict]) -> list[dict]:
    """批量标注。跳过无效样本。"""
    results = []
    for s in samples:
        labeled = label_sample(s.get("text", ""), s.get("entities", []))
        if labeled:
            results.append(labeled)
    if results:
        logger.info("ner_labeler: %d/%d samples labeled", len(results), len(samples))
    return results
