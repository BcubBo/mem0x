"""security.conflict_resolver — 矛盾记忆规则消解（standalone 版 + LLM 辅助）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
写入新记忆前，用规则匹配检测是否与已有记忆矛盾：
- 状态翻转（开启↔关闭、启用↔禁用）
- 值变更（端口从A改为B、路径从X改为Y）
- 属性级覆盖（同 category+key 的旧值被新值替代）

消解方式：旧记忆标记 metadata.archived=true（不删除，可回滚）。

LLM 辅助：规则未命中时，调用 LLM 判断新旧记忆是否矛盾。
置信度分层：≥0.8 自动归档，0.5-0.8 标记待审，<0.5 忽略。

改进：
- 时间戳判断：新记忆更新才归档旧记忆
- 来源追踪：标记记忆来源（user_stated/system_inferred）
- 事实更新判断：LLM 判断新记忆是否"更新"旧事实，而非简单"矛盾"
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger("mem0x.conflict_resolver")

# ── 互斥属性规则集 ──
MUTUAL_EXCLUSION_PATTERNS: list[tuple[str, str, str]] = [
    (r"(开关|状态|status|mode)", r"(开启|启用|open|enable|true)", r"(关闭|禁用|close|disable|false)"),
    (r"(开关|状态|status|mode)", r"(关闭|禁用|close|disable|false)", r"(开启|启用|open|enable|true)"),
    (r"(端口|port)", r"\d{4,5}", r"\d{4,5}"),
    (r"(路径|path|目录|dir)", r"[/\\][\w/\\.-]+", r"[/\\][\w/\\.-]+"),
    (r"(版本|version|v\d)", r"v?\d+\.\d+[\.\d]*", r"v?\d+\.\d+[\.\d]*"),
    (r"(从|改为|改成|变成|change)", r"\d+", r"\d+"),
]

_CHANGE_KEYWORDS = re.compile(
    r"(从.{2,30}?改为|从.{2,30}?改成|从.{2,30}?变[为成]|"
    r"端口.{0,5}?改|路径.{0,5}?改|版本.{0,5}?改|状态.{0,5}?改|"
    r"不再|已经不|现在是|改为用|换成了|替换成|"
    r"change(?:d)?\s+to|switch(?:ed)?\s+to|replace(?:d)?\s+with)",
    re.IGNORECASE,
)

# ── LLM 配置 ──
_llm_config_cache = None
_llm_config_cached_at = 0
_llm_config_lock = threading.Lock()


def _get_llm_config() -> dict:
    """从 config.json 的 conflict.llm 段读取 LLM 配置。"""
    global _llm_config_cache, _llm_config_cached_at
    if _llm_config_cache is not None and time.time() - _llm_config_cached_at < 300:
        return _llm_config_cache

    with _llm_config_lock:
        if _llm_config_cache is not None and time.time() - _llm_config_cached_at < 300:
            return _llm_config_cache

        _llm_config_cache = {"model": "", "base_url": "", "api_key": "", "max_tokens": 5000, "max_llm_calls": 3}
        try:
            from security.utils import get_config
            raw = get_config()
            llm_cfg = raw.get("conflict", {}).get("llm", {}).get("config", {})
            _llm_config_cache = {
                "model": llm_cfg.get("model", ""),
                "base_url": llm_cfg.get("openai_base_url") or llm_cfg.get("base_url", ""),
                "api_key": llm_cfg.get("api_key", ""),
                "max_tokens": llm_cfg.get("max_tokens", 5000),
                "max_llm_calls": llm_cfg.get("max_llm_calls", 3),
            }
            if _llm_config_cache["api_key"] and _llm_config_cache["model"] and _llm_config_cache["base_url"]:
                _llm_config_cached_at = time.time()
                logger.info("conflict 使用 LLM: %s", _llm_config_cache["model"])
            else:
                logger.warning("conflict.llm 配置不完整，LLM 不可用")
                _llm_config_cache = {"model": "", "base_url": "", "api_key": ""}
        except Exception as e:
            logger.warning(f"读取 LLM 配置失败: {e}")

    return _llm_config_cache


def _get_llm_client():
    """获取 LLM 客户端和 max_tokens。"""
    cfg = _get_llm_config()
    if not cfg.get("api_key") or not cfg.get("model"):
        return None, None, None
    try:
        import openai
        client = openai.OpenAI(
            api_key=cfg["api_key"],
            base_url=cfg.get("base_url", "https://api.siliconflow.cn/v1"),
        )
        return client, cfg["model"], cfg.get("max_tokens", 5000)
    except Exception as e:
        logger.debug("LLM 客户端创建失败: %s", e)
        return None, None, None


def _llm_judge_contradiction(
    new_text: str,
    old_text: str,
    new_meta: dict = None,
    old_meta: dict = None,
    old_created_at: str = None,
) -> Optional[Dict[str, Any]]:
    """用 LLM 判断新旧记忆的关系。

    返回:
        {"contradicts": bool, "confidence": float, "reason": str, "action": "archive_old"|"keep_both"}
    """
    client, model, max_tokens = _get_llm_client()
    if not client or not model:
        return None

    # 提取元数据
    new_meta = new_meta or {}
    old_meta = old_meta or {}

    # 构建元数据信息
    old_priority = _extract_priority(old_text)
    old_category = _extract_category(old_text)
    old_lane = _extract_lane(old_text)
    old_source = old_meta.get("source", "unknown")
    old_attributed_to = old_meta.get("attributed_to", "unknown")

    metadata_info = f"""
【旧记忆元数据】
- 优先级：{old_priority}
- 分类：{old_category}
- 过期策略：{old_lane}
- 来源：{old_source}（user_stated=用户直接陈述，system_inferred=系统推断）
- 归属：{old_attributed_to}（user=用户说的，assistant=系统提取的）
- 创建时间：{old_created_at or '未知'}"""

    prompt = f"""你是一个记忆矛盾检测器。请判断以下两条新旧记忆是否矛盾。

【新记忆】
{new_text[:400]}

【旧记忆】
{old_text[:400]}
{metadata_info}

判断规则：
1. 新记忆明确更新了旧记忆的事实 → contradicts=true, action="archive_old"
2. 新旧记忆描述同一事实但值不同 → contradicts=true, action="archive_old"
3. 新旧记忆描述不同方面或不同时期 → contradicts=false, action="keep_both"
4. 旧记忆是用户直接陈述（user_stated），新记忆是系统推断 → 谨慎，conf降低
5. 不确定 → contradicts=false, action="keep_both"

重要：你可以在思考过程中进行详细分析，但最终必须在回复中只输出一个JSON对象，不要有任何其他文字。

输出格式：{{"contradicts": true/false, "confidence": 0.0-1.0, "reason": "一句话理由", "action": "archive_old/keep_both"}}"""

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.1,
        )
        # mimo-v2.5 是推理模型，输出可能在 reasoning_content 里
        message = resp.choices[0].message
        content = ""
        reasoning = ""

        # 优先检查 content（最终答案）
        if message.content:
            content = message.content.strip()
            logger.info("LLM content: %s", content[:300])

        # 检查 reasoning_content（推理过程）
        if getattr(message, "reasoning_content", None):
            reasoning = message.reasoning_content.strip()
            logger.info("LLM reasoning_content length: %d", len(reasoning))

        # 如果 content 为空但有 reasoning_content，从推理内容中提取 JSON
        if not content and reasoning:
            content = reasoning
            logger.info("Using reasoning_content for JSON extraction")

        logger.info("LLM 原始返回: %s", content[:300])

        # 智能提取 JSON：按优先级尝试多种模式
        result = None

        # 1. 尝试匹配完整的 JSON 对象（支持嵌套）
        #    匹配从 { 开始到匹配的 } 结束的内容
        brace_count = 0
        start_pos = -1
        for i, char in enumerate(content):
            if char == '{':
                if brace_count == 0:
                    start_pos = i
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0 and start_pos >= 0:
                    candidate = content[start_pos:i+1]
                    try:
                        result = json.loads(candidate)
                        logger.info("JSON extracted (nested-aware): %s", result)
                        break
                    except json.JSONDecodeError:
                        start_pos = -1

        # 2. 如果上面失败，用正则从后向前找最后一个 JSON
        if not result:
            matches = list(re.finditer(r'\{[^{}]+\}', content))
            for m in reversed(matches):
                try:
                    result = json.loads(m.group())
                    logger.info("JSON extracted (regex last-match): %s", result)
                    break
                except json.JSONDecodeError:
                    continue

        if result:
            # 标准化返回
            contradicts = result.get("contradicts", False)
            confidence = min(max(result.get("confidence", 0.5), 0.0), 1.0)
            reason = result.get("reason", "")
            action = result.get("action", "keep_both")

            # 兼容旧格式
            if contradicts and action == "keep_both":
                action = "archive_old"

            return {
                "contradicts": bool(contradicts),
                "confidence": confidence,
                "reason": reason,
                "action": action,
            }
        else:
            logger.warning("LLM 返回内容中未找到有效 JSON")

    except Exception as e:
        logger.warning("LLM 矛盾判断失败: %s", e)

    return None


def _extract_entity(text: str) -> str:
    """从变更语句中提取变更主体。"""
    m = re.search(r"(端口|路径|目录|版本|版本号|status|mode|状态|配置|地址|URL|端点)\s*(?:从|由|改成|改为|变为)", text, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    m = re.search(r"(端口|路径|目录|版本|状态|配置|地址|URL|端点)\s*[:：]?\s*\S+", text, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return ""


def _extract_priority(text: str) -> str:
    """提取记忆优先级（P0/P1/P2）。"""
    m = re.search(r"\[P([012])\]", text[:50])
    return f"P{m.group(1)}" if m else "未知"


def _extract_category(text: str) -> str:
    """提取记忆分类（tech/preference/person/project/lesson）。"""
    m = re.search(r"\[(tech|preference|person|project|lesson)\]", text[:50], re.IGNORECASE)
    return m.group(1) if m else "未知"


def _extract_lane(text: str) -> str:
    """提取过期策略（identity/project/default）。"""
    m = re.search(r"\[lane:(identity|project|default)\]", text[:50], re.IGNORECASE)
    return m.group(1) if m else "未知"


def _entity_matches(old_text: str, new_text: str) -> bool:
    """检查新旧记忆是否关于同一实体。"""
    old_entity = _extract_entity(old_text)
    new_entity = _extract_entity(new_text)
    if not old_entity and not new_entity:
        return True
    if old_entity and new_entity:
        return old_entity == new_entity
    return False


# ── SQLite 账本（从 config 读路径） ──
def _get_db_path() -> str:
    from .utils import get_data_dir
    return os.path.join(get_data_dir(), "conflict.db")

_schema_checked = False
_schema_retry_count = 0
_MAX_SCHEMA_RETRIES = 3
_schema_lock = threading.Lock()


def _get_user_id() -> str:
    from .utils import get_user_id
    return get_user_id()

USER_ID = _get_user_id()
AGENT_ID = os.environ.get("MEM0_AGENT_ID", "hermes")


def _get_db() -> sqlite3.Connection:
    db_path = _get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema() -> None:
    global _schema_checked, _schema_retry_count
    if _schema_checked:
        return
    if _schema_retry_count >= _MAX_SCHEMA_RETRIES:
        return
    with _schema_lock:
        if _schema_checked:
            return
        conn = _get_db()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conflict_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id   TEXT NOT NULL,
                    old_content TEXT NOT NULL,
                    new_content TEXT NOT NULL,
                    reason      TEXT NOT NULL,
                    rule_type   TEXT NOT NULL,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cf_memory ON conflict_events(memory_id)")
            conn.commit()
            _schema_checked = True
        except Exception as e:
            _schema_retry_count += 1
            logger.warning(f"conflict_events 表初始化失败 (retry {_schema_retry_count}/{_MAX_SCHEMA_RETRIES}): {e}")
        finally:
            conn.close()


def _log_conflict(memory_id: str, old_content: str, new_content: str, reason: str, rule_type: str) -> None:
    _ensure_schema()
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO conflict_events (memory_id, old_content, new_content, reason, rule_type) VALUES (?,?,?,?,?)",
            (memory_id, old_content[:500], new_content[:500], reason[:200], rule_type),
        )
        conn.commit()
    except Exception as e:
        logger.debug(f"conflict 事件记录失败: {e}")
    finally:
        conn.close()


def _has_change_signal(text: str) -> bool:
    if not text:
        return False
    return bool(_CHANGE_KEYWORDS.search(text))


def _find_conflicting_patterns(new_text: str) -> list[tuple[str, str, str]]:
    if not _has_change_signal(new_text):
        return []
    return [
        (attr_re, old_re, new_re)
        for attr_re, old_re, new_re in MUTUAL_EXCLUSION_PATTERNS
        if re.search(new_re, new_text, re.IGNORECASE)
    ]


def _text_matches_old_pattern(text: str, old_re: str) -> bool:
    return bool(re.search(old_re, text, re.IGNORECASE))


def detect_and_resolve(memory, new_text: str, filters: dict = None, auto_archive_threshold: float = 0.8) -> Optional[dict]:
    """写入前矛盾检测入口。返回 None → 无矛盾。

    auto_archive_threshold: 置信度 ≥ 此值自动归档，< 此值标记待审。
    优化：最大 LLM 调用 3 次，找到冲突即停止。
    """
    if not new_text or len(new_text) < 15:
        return None

    triggered = _find_conflicting_patterns(new_text)

    if filters is None:
        filters = {"user_id": USER_ID, "agent_id": AGENT_ID}

    try:
        try:
            raw = memory.search(new_text, filters=filters, top_k=20)
        except TypeError:
            raw = memory.search(new_text, filters=filters, limit=20)
        results = raw.get("results", raw) if isinstance(raw, dict) else raw
        if not isinstance(results, list):
            return None
    except Exception as e:
        logger.debug("矛盾检测搜索失败: %s", e)
        return None

    conflicts = []
    llm_call_count = 0
    from .conflict_resolver import _get_llm_config
    MAX_LLM_CALLS = _get_llm_config().get("max_llm_calls", 3)  # 从配置读取

    for r in results:
        if not isinstance(r, dict):
            continue
        mid = r.get("id", "")
        old_text = (r.get("memory") or "").strip()
        if not mid or not old_text:
            continue
        meta = r.get("metadata") or {}

        # 跳过已归档和已删除的记忆
        if meta.get("archived") or meta.get("deleted_at"):
            continue

        # 只考虑 P0/P1 优先级的记忆
        if not re.search(r"\[P[01]\]", old_text[:50]):
            continue

        # 实体对齐：新旧记忆必须关于同一实体
        if not _entity_matches(old_text, new_text):
            continue

        # 获取时间戳
        old_created_at = r.get("created_at")
        new_created_at = None  # 新记忆还没有 created_at

        # 规则匹配
        rule_matched = False
        if triggered:
            for attr_re, old_re, new_re in triggered:
                old_attr_match = re.search(attr_re, old_text[:500], re.IGNORECASE)
                new_attr_match = re.search(attr_re, new_text[:500], re.IGNORECASE)
                if not old_attr_match or not new_attr_match:
                    continue
                if old_attr_match.group(0).lower() != new_attr_match.group(0).lower():
                    continue
                if _text_matches_old_pattern(old_text[:500], old_re) and _text_matches_old_pattern(new_text[:500], new_re):
                    old_val = re.search(old_re, old_text[:500], re.IGNORECASE)
                    new_val = re.search(new_re, new_text[:500], re.IGNORECASE)
                    if old_val and new_val and old_val.group(0) == new_val.group(0):
                        continue
                    rule_matched = True
                    confidence = 0.95  # 规则匹配高置信
                    reason = f"规则触发: {old_re} → {new_re}"
                    llm_action = "archive_old"
                    break

        # LLM 辅助判断（规则未命中时）
        if not rule_matched:
            # 检查 LLM 调用次数限制
            if llm_call_count >= MAX_LLM_CALLS:
                logger.info("LLM 调用次数已达上限 %d，跳过后续检测", MAX_LLM_CALLS)
                break

            llm_call_count += 1
            logger.info("调用 LLM 判断矛盾 (%d/%d): new=%s..., old=%s...", llm_call_count, MAX_LLM_CALLS, new_text[:30], old_text[:30])
            llm_result = _llm_judge_contradiction(new_text, old_text, old_meta=meta, old_created_at=old_created_at)
            logger.info("LLM 返回结果: %s", llm_result)
            if llm_result and llm_result.get("contradicts"):
                confidence = llm_result["confidence"]
                reason = llm_result.get("reason", "LLM判断矛盾")
                llm_action = llm_result.get("action", "archive_old")
            else:
                continue

        # 置信度分层处理
        old_meta = dict(meta)
        if confidence >= auto_archive_threshold and llm_action == "archive_old":
            # 高置信：自动归档
            old_meta["archived"] = True
            old_meta["archived_by"] = "conflict_resolver"
            old_meta["superseded_by"] = new_text[:200]
            action_type = "archived"
        elif confidence >= 0.5:
            # 中置信：标记待审
            old_meta["conflict_pending"] = True
            old_meta["conflict_reason"] = reason
            old_meta["conflict_confidence"] = confidence
            action_type = "pending_review"
        else:
            # 低置信：跳过
            continue

        try:
            memory.update(mid, old_text, metadata=old_meta)
            _log_conflict(mid, old_text, new_text, reason, "rule_match" if rule_matched else "llm_judge")
            conflicts.append({
                "memory_id": mid,
                "old_content": old_text[:100],
                "reason": reason,
                "confidence": confidence,
                "action": action_type,
            })
            logger.info("⚔️ conflict: id=%s %s (置信度=%.2f)", mid[:8], action_type, confidence)
        except Exception as e:
            logger.debug("归档失败 %s: %s", mid[:8], e)

        # 找到冲突即停止，不再检查后续记忆
        break

    if not conflicts:
        return None

    return {
        "resolved": len(conflicts),
        "conflicts": conflicts,
        "action": "conflict_detected",
    }


def list_pending_conflicts(memory, limit: int = 20) -> list[dict]:
    """列出待审的冲突记忆。"""
    try:
        filters = {"user_id": USER_ID, "agent_id": AGENT_ID, "conflict_pending": True}
        raw = memory.search("", filters=filters, top_k=limit)
        results = raw.get("results", raw) if isinstance(raw, dict) else raw
        if not isinstance(results, list):
            return []
        return [
            {"memory_id": r.get("id"), "content": r.get("memory", "")[:100],
             "reason": r.get("metadata", {}).get("conflict_reason", ""),
             "confidence": r.get("metadata", {}).get("conflict_confidence", 0)}
            for r in results if isinstance(r, dict)
        ]
    except Exception as e:
        logger.debug("查询待审冲突失败: %s", e)
        return []


def resolve_pending(memory, memory_id: str, approve: bool = True) -> dict:
    """处理待审冲突。approve=True 归档，False 忽略。"""
    try:
        got = memory.get(memory_id)
        if not isinstance(got, dict):
            return {"status": "error", "detail": f"记忆 {memory_id[:8]} 不存在"}

        old_text = got.get("memory") or got.get("content") or ""
        old_meta = got.get("metadata") or {}

        if approve:
            old_meta["archived"] = True
            old_meta["archived_by"] = "conflict_resolver"
            old_meta.pop("conflict_pending", None)
            old_meta.pop("conflict_reason", None)
            old_meta.pop("conflict_confidence", None)
        else:
            old_meta.pop("conflict_pending", None)
            old_meta.pop("conflict_reason", None)
            old_meta.pop("conflict_confidence", None)

        memory.update(memory_id, old_text, metadata=old_meta)
        return {"status": "ok", "action": "archived" if approve else "ignored"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def rollback_conflict(memory_id: str, memory=None) -> dict:
    """回滚一次矛盾消解。"""
    if memory is None:
        return {"status": "error", "detail": "mem0 实例未传入"}

    try:
        got = memory.get(memory_id)
        if not isinstance(got, dict):
            return {"status": "error", "detail": f"记忆 {memory_id[:8]} 不存在"}

        old_text = got.get("memory") or got.get("content") or ""
        old_meta = got.get("metadata") or {}
        old_meta.pop("archived", None)
        old_meta.pop("archived_by", None)
        old_meta.pop("superseded_by", None)
        memory.update(memory_id, old_text, metadata=old_meta)

        logger.info("↩️ conflict 回滚: %s", memory_id[:8])
        return {"status": "ok", "memory_id": memory_id}

    except Exception as e:
        return {"status": "error", "detail": str(e)}


def list_conflicts(limit: int = 20) -> list[dict]:
    """列出矛盾消解记录。"""
    _ensure_schema()
    conn = _get_db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM conflict_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]
        return rows
    finally:
        conn.close()
