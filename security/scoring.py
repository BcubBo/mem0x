"""六维打分 + Ignition — 搜索结果后处理引擎。


功能：
1. 六维打分（向量 + BM25 + 时间衰减 + 可靠性 + 热度）
2. Ignition：相似度 >0.85 的记忆跳过衰减，直达返回
3. 去重：相同文本记忆去重
"""
from __future__ import annotations
import os

import logging
import math
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("mem0.scoring")

# ── 配置 ──
try:
    RECENCY_LAMBDA = float(os.environ.get("MEM0_RECENCY_LAMBDA", "0.05"))
except (ValueError, TypeError):
    RECENCY_LAMBDA = 0.05
IGNITION_THRESHOLD = 0.85
IGNITION_MAX = 8

DEFAULT_WEIGHTS = {
    "vector": 0.38,
    "time": 0.15,
    "reliability": 0.10,
    "heat": 0.17,
    "confidence": 0.20,
}



_FACT_KEYWORDS = re.compile(
    r"生日|是谁|哪天|什么时候|喜欢|偏好|最爱|爱好|习惯|底线|规则|铁律|是什么|配置|账号|密码|邮箱|电话|微信|身份|关系",
    re.IGNORECASE,
)


def _normalize_score(score: Any) -> float:
    if score is None:
        return 0.0
    try:
        s = float(score)
        if s < 0:
            return 0.0
        if s > 1:
            return 1.0
        return s
    except (ValueError, TypeError):
        return 0.0




def _extract_timestamp(item: dict) -> float:
    """三级时间戳提取。"""
    if not isinstance(item, dict):
        return 0.0
    for key in ("timestamp", "created_at", "recorded_at", "updated_at"):
        val = item.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
        if isinstance(val, str) and val.strip():
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
    md = item.get("metadata") or {}
    if isinstance(md, dict):
        for key in ("timestamp", "created_at", "recorded_at", "updated_at"):
            val = md.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return float(val)
    return 0.0


def _compute_time_decay(created_ts: float, now_ts: Optional[float] = None,
                        recency_lambda: Optional[float] = None) -> float:
    if created_ts <= 0:
        return 0.5
    now = now_ts or time.time()
    age_days = max(0.0, (now - created_ts) / 86400.0)
    lam = recency_lambda if recency_lambda is not None else RECENCY_LAMBDA
    return round(math.exp(-lam * age_days), 4)


def _compute_confidence(created_ts: float, access_count: float = 0,
                        now_ts: Optional[float] = None) -> float:
    """置信度衰减：越老越低，访问越多越高。

    公式：base_decay * access_boost
    - base_decay: 指数衰减，半衰期 ~90 天（比 time 衰减慢）
    - access_boost: log(1 + access_count) / 5.0，上限 1.0
    """
    if created_ts <= 0:
        return 0.5
    now = now_ts or time.time()
    age_days = max(0.0, (now - created_ts) / 86400.0)
    base_decay = math.exp(-0.008 * age_days)  # 半衰期 ~87 天
    access_boost = min(1.0, math.log1p(access_count) / 5.0)
    return round(base_decay * (0.5 + 0.5 * access_boost), 4)


def score_and_rank(
    query: str,
    candidates: List[dict],
    *,
    weights: Optional[Dict[str, float]] = None,
    limit: int = 10,
    config: Optional[Dict[str, Any]] = None,
) -> List[dict]:
    """六维打分 + Ignition + 去重。

    返回排好序的结果列表，每条加 _hybrid_score 和 _time_decay 字段。
    权重优先级：weights 参数 > config.json scoring.weights > DEFAULT_WEIGHTS
    """
    if not candidates:
        return []

    # 从 config 读取权重配置
    cfg_weights = (config or {}).get("scoring", {}).get("weights", {})
    w = {**DEFAULT_WEIGHTS, **cfg_weights, **(weights or {})}

    # 从 config 读取 recency_lambda（不修改全局变量，线程安全）
    cfg_lambda = (config or {}).get("scoring", {}).get("recency_lambda")
    try:
        _lam = float(cfg_lambda) if cfg_lambda is not None else None
    except (ValueError, TypeError):
        _lam = None
    now_ts = time.time()
    is_fact_query = bool(_FACT_KEYWORDS.search(query))

    scored = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        item = dict(item)  # 复制，不污染调用方原始数据

        # 跳过已归档的记忆（精炼合并后的旧记忆）
        meta = item.get("metadata") or {}
        if meta.get("archived"):
            continue

        # 向量分
        vec_s = _normalize_score(item.get("score", 0) or item.get("rerank_score", 0))

        # 时间衰减分
        created_ts = _extract_timestamp(item)
        time_s = _compute_time_decay(created_ts, now_ts, recency_lambda=_lam)

        # 可靠性分（从 metadata 或默认 0.5）
        try:
            reliability_s = float((item.get("metadata") or {}).get("reliability", 0.5))
        except (ValueError, TypeError):
            reliability_s = 0.5

        # 热度分：由 salience boost 统一注入，此处不算（避免双重计算）
        raw_count = (item.get("metadata") or {}).get("access_count", 1)
        access_count = float(raw_count if raw_count is not None else 1)
        heat_s = 0.0

        # 置信度衰减分
        confidence_s = _compute_confidence(created_ts, access_count, now_ts)

        # 综合得分
        base_score = (
            w["vector"] * vec_s
            + w["time"] * time_s
            + w["reliability"] * reliability_s
            + w["heat"] * heat_s
            + w["confidence"] * confidence_s
        )

        # 事实类查询增益（cap 1.0）
        if is_fact_query:
            base_score = min(base_score * 1.2, 1.0)

        item["_hybrid_score"] = round(base_score, 4)
        item["_time_decay"] = round(time_s, 4)
        scored.append(item)

    # Ignition：rerank_score > 阈值的直达
    ignited = [s for s in scored if _normalize_score(s.get("rerank_score", 0)) >= IGNITION_THRESHOLD]
    remaining = [s for s in scored if _normalize_score(s.get("rerank_score", 0)) < IGNITION_THRESHOLD]

    # 去重（相同 memory 文本）
    seen = set()
    deduped = []
    for item in ignited + remaining:
        text = str(item.get("memory") or item.get("text") or "")[:200]
        if text in seen:
            continue
        seen.add(text)
        deduped.append(item)

    # Ignited 强制排在前面，内部按 hybrid_score 排序
    ignited_set = {id(s) for s in ignited}
    deduped.sort(key=lambda x: (0 if id(x) in ignited_set else 1, -x.get("_hybrid_score", 0)))

    result = deduped[:limit]
    logger.debug(
        "Scoring: %d candidates → %d ignited, %d deduped → %d final",
        len(candidates), len(ignited), len(deduped), len(result),
    )
    return result
