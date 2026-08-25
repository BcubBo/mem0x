"""security.pipeline — 写入链路编排（standalone 版）

完整链路：注入防御→PII脱敏→搜索候选→矛盾消解→Jaccard去重→存入
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .injection_guard import validate_memory_content
from .dedup import find_duplicate
from .conflict_resolver import detect_and_resolve

logger = logging.getLogger("mem0x.pipeline")

# ── 脱敏配置（从 config.json 加载，不硬编码） ──
_REDACT_MAP: dict[str, str] = {}
_REDACT_RE = None


def load_redact_names(config: dict) -> None:
    """从 config.json 的 redact_names 字段加载脱敏映射。

    config.json 示例：
    {
      "redact_names": {
        "真实姓名": "脱敏代号"
      }
    }
    """
    global _REDACT_MAP, _REDACT_RE
    names = config.get("redact_names", {})
    if names:
        _REDACT_MAP = dict(names)
        _REDACT_RE = re.compile(
            "|".join(re.escape(k) for k in sorted(_REDACT_MAP, key=len, reverse=True))
        )
        logger.info("loaded %d redact names from config", len(_REDACT_MAP))

# ── PII 正则（身份证/手机/邮箱） ──
# 18位身份证：6位地区 + 8位生日 + 3位顺序 + 1位校验
_ID_CARD_RE = re.compile(r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")
# 11位手机号：1[3-9]开头，前后不能紧跟数字（兼容中文环境，不用\b）
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 邮箱
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
# 密码明文模式（常见标记后的值）
_PASSWORD_RE = re.compile(
    r"(密码|password|passwd|secret|token|api[_\-]?key)\s*[:：=]\s*\S+",
    re.IGNORECASE,
)


def add_redact_name(name: str, replacement: str) -> None:
    """动态添加脱敏映射。"""
    _REDACT_MAP[name] = replacement
    global _REDACT_RE
    _REDACT_RE = re.compile(
        "|".join(re.escape(k) for k in sorted(_REDACT_MAP, key=len, reverse=True))
    )


def redact_pii(text: str) -> str:
    """脱敏处理：姓名替换 + PII 拦截（身份证/手机/邮箱/密码）。

    姓名：替换为脱敏名称
    身份证/手机/邮箱：替换为 [REDACTED_XXX]
    密码明文：替换为 [REDACTED_PWD]
    """
    if not text:
        return text

    result = text

    # 1. 姓名替换
    if _REDACT_RE:
        result = _REDACT_RE.sub(lambda m: _REDACT_MAP[m.group()], result)

    # 2. PII 拦截
    pii_found = False
    if _ID_CARD_RE.search(result):
        result = _ID_CARD_RE.sub("[REDACTED_ID]", result)
        pii_found = True
    if _PHONE_RE.search(result):
        result = _PHONE_RE.sub("[REDACTED_PHONE]", result)
        pii_found = True
    if _EMAIL_RE.search(result):
        result = _EMAIL_RE.sub("[REDACTED_EMAIL]", result)
        pii_found = True
    if _PASSWORD_RE.search(result):
        result = _PASSWORD_RE.sub(r"\1=[REDACTED_PWD]", result)
        pii_found = True

    if result != text:
        logger.info("PII redacted (pii=%s)", pii_found)
    return result


async def safe_add(
    memory,
    content: str,
    filters: dict = None,
    *,
    user_id: str = None,
    agent_id: str = None,
    metadata: dict = None,
    expiration_date: str = None,
    infer: bool = False,
) -> dict:
    """安全写入链路：注入防御→脱敏→去重→矛盾消解→语义判重→存入。

    Args:
        memory: mem0 Memory 实例
        content: 要写入的文本
        filters: mem0 search filters
        user_id/agent_id: 写入身份
        metadata: 附加 metadata
        expiration_date: 过期日期 YYYY-MM-DD
        infer: 是否用 LLM 提取事实（默认 False，因为 pipeline 自己做判重）

    Returns:
        {"action": "added"|"duplicate"|"conflict"|"semantic"|"rejected", ...}
    """
    logger.info("🔗 pipeline.safe_add: content_len=%d, user=%s, agent=%s", len(content), user_id, agent_id)
    
    # 1. 注入防御
    is_valid, content, reject_reason = validate_memory_content(content)
    if not is_valid or not content:
        logger.warning("🛡️ pipeline.rejected: reason=%s", reject_reason)
        return {"action": "rejected", "reason": reject_reason or "empty content"}

    # 1.5 PII 脱敏（必须在搜索之前，确保搜索用脱敏文本）
    content = redact_pii(content)

    # 2. 搜索一次（共享给 dedup，省一次 embedder 调用）
    if filters is None:
        filters = {}
        if user_id:
            filters["user_id"] = user_id
        if agent_id:
            filters["agent_id"] = agent_id
    # mem0 2.0+ 要求 filters 至少有一个 entity ID
    if not filters.get("user_id") and not filters.get("agent_id") and not filters.get("run_id"):
        filters["user_id"] = "bo"  # 默认用户

    shared_results = []
    try:
        raw = await memory.search(content, filters=filters, top_k=5)
        shared_results = raw.get("results", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    except Exception:
        pass

    # 3. 矛盾消解（规则驱动，零 LLM 成本，优先于 dedup）
    conflict_result = await detect_and_resolve(memory, content, filters=filters, pre_results=shared_results)
    if conflict_result:
        logger.info("⚔️ pipeline.conflict: resolved=%d", conflict_result.get("resolved", 0))
        # 矛盾消解后，用 Jaccard 快速去重（不调 LLM，省~30秒）
        dup = await find_duplicate(memory, content, filters, _pre_results=shared_results)
        if dup:
            mem_id, old_text, sim = dup
            logger.info("🔄 pipeline.duplicate_after_conflict: memory_id=%s, similarity=%.2f", mem_id, sim)
            try:
                await memory.update(mem_id, content)
            except Exception as e:
                logger.warning("dedup update 失败: %s", e)
                return {"action": "error", "reason": f"dedup update failed: {e}"}
            return {"action": "duplicate", "memory_id": mem_id, "similarity": sim}
        # 语义判重通过，写入新记忆
        try:
            add_kwargs = {
                "user_id": user_id,
                "agent_id": agent_id,
                "infer": infer,
                "metadata": metadata,
            }
            if expiration_date is not None:
                add_kwargs["expiration_date"] = expiration_date
            result = await memory.add(
                [{"role": "user", "content": content}],
                **add_kwargs,
            )
            results = result.get("results", []) if isinstance(result, dict) else []
            memory_id = results[0].get("id") if results else None
            logger.info("✅ pipeline.added_after_conflict: memory_id=%s", memory_id)
        except Exception as e:
            logger.warning("safe_add conflict后写入异常: %s", e)
            memory_id = None
        return {
            "action": "conflict", "resolved": conflict_result["resolved"],
            "conflicts": conflict_result["conflicts"], "memory_id": memory_id,
        }

    # 4. Jaccard 去重（用共享结果，在 conflict 之后）
    dup = await find_duplicate(memory, content, filters, _pre_results=shared_results)
    if dup:
        mem_id, old_text, sim = dup
        logger.info("🔄 pipeline.duplicate: memory_id=%s, similarity=%.2f", mem_id, sim)
        try:
            await memory.update(mem_id, content)
        except Exception as e:
            logger.warning("dedup update 失败: %s", e)
            return {"action": "error", "reason": f"dedup update failed: {e}"}
        # FTS5 同步：dedup 更新了内容，同步到 FTS5
        try:
            from wrapper.fts5_store import get_fts5
            get_fts5().write(mem_id, content, user_id)
        except Exception as e:
            logger.debug("FTS5 dedup sync 失败: %s", e)
        return {"action": "duplicate", "memory_id": mem_id, "similarity": sim}

    # 5. 正常写入
    try:
        add_kwargs = {
            "user_id": user_id,
            "agent_id": agent_id,
            "infer": infer,
            "metadata": metadata,
        }
        if expiration_date is not None:
            add_kwargs["expiration_date"] = expiration_date
        result = await memory.add(
            [{"role": "user", "content": content}],
            **add_kwargs,
        )
        results = result.get("results", []) if isinstance(result, dict) else []
        memory_id = results[0].get("id") if results else None
        logger.info("✅ pipeline.added: memory_id=%s", memory_id)
        return {"action": "added", "memory_id": memory_id}
    except Exception as e:
        logger.warning("safe_add 异常: %s", e)
        return {"action": "error", "reason": str(e)}
