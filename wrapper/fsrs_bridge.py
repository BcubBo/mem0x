"""FSRS 适配层：将标准 FSRS-6 库映射到 mem0x 记忆管理。

Card → 每条记忆
Rating.Good → 记忆被搜索/使用
Rating.Again → 记忆长期未使用
Stability → 记忆保持强度
Difficulty → 记忆内在难度
Retrievability → 当前可检索概率
"""
import json
import logging
import math
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from fsrs import Scheduler, Card, Rating, State

logger = logging.getLogger("mem0x.fsrs_bridge")

# 全局 Scheduler 单例（默认参数，后续可优化）
_scheduler = Scheduler()


def card_from_metadata(metadata: dict, created_at: str = None) -> Card:
    """从记忆 metadata 反序列化 FSRS Card。

    如果 metadata 中没有 fsrs_card 字段，返回默认新 Card。
    """
    card_json = metadata.get("fsrs_card")
    if card_json:
        try:
            if isinstance(card_json, str):
                card_json = json.loads(card_json)
            return Card.from_json(card_json)
        except Exception as e:
            logger.debug("FSRS Card 反序列化失败: %s", e)

    # 默认 Card：从创建时间开始
    card = Card()
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            card.due = dt
            card.last_review = dt
        except Exception as e:
            logger.debug("FSRS Card datetime parse: %s", e)
    return card


def card_to_metadata(card: Card) -> dict:
    """将 FSRS Card 序列化为 metadata 字段。"""
    return {"fsrs_card": card.to_json()}


def compute_retrievability(metadata: dict, created_at: str = None) -> float:
    """计算记忆的当前可检索概率 R(t, S)。"""
    card = card_from_metadata(metadata, created_at)
    return _scheduler.get_card_retrievability(card)


def record_access(metadata: dict, created_at: str = None) -> dict:
    """记录记忆被搜索/使用，更新 Card 状态（Rating.Good）。"""
    card = card_from_metadata(metadata, created_at)
    card, review_log = _scheduler.review_card(card, Rating.Good)
    return card_to_metadata(card)


def record_stale(metadata: dict, created_at: str = None) -> dict:
    """标记记忆长期未使用（Rating.Again）。"""
    card = card_from_metadata(metadata, created_at)
    card, review_log = _scheduler.review_card(card, Rating.Again)
    return card_to_metadata(card)


def get_quality_score(metadata: dict, created_at: str = None,
                       access_count: int = 0) -> float:
    """计算记忆质量综合分数 [0, 1]。

    综合考虑：
    - Retrievability R：可检索概率（权重 0.5）
    - Stability S：归一化到 [0,1]（权重 0.3）
    - Access boost：访问次数加成（权重 0.2）

    旧记忆（无 fsrs_card）使用 age-based 基线分数，避免全部被判为低质。
    """
    has_fsrs_card = bool(metadata.get("fsrs_card"))

    if has_fsrs_card:
        card = card_from_metadata(metadata, created_at)
        R = _scheduler.get_card_retrievability(card)
        S = card.stability or 1.0
        S_norm = min(S / 30.0, 1.0)
    else:
        # 旧记忆无 fsrs_card：用 age-based 基线
        # 新记忆（<7天）R=0.8，老记忆（>30天）R=0.5
        age_days = _age_days(created_at)
        R = max(0.5, 1.0 - age_days / 60.0)  # 60天衰减到0.5
        S_norm = min(age_days / 30.0, 1.0)  # 老记忆稳定性更高

    access_boost = min(math.log1p(access_count) / 5.0, 1.0)
    Q = 0.5 * R + 0.3 * S_norm + 0.2 * access_boost
    return round(Q, 4)


def _age_days(created_at: str = None) -> float:
    """计算记忆年龄（天）。"""
    if not created_at:
        return 0.5
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max((datetime.now(timezone.utc) - dt).days, 0)
    except Exception as e:
        logger.debug("FSRS age_days parse: %s", e)
        return 0.5
