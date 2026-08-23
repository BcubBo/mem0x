"""security.self_edit — 记忆语义判重（standalone 版）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
写入新记忆前，用 LLM 判断与既有记忆是「重复/冲突/全新」：
- 重复 → 合并（update 旧记忆，不新增）
- 冲突 → 保留双方，标注时间
- 全新 → 正常新增

与 dedup.py (Bigram Jaccard) 的关系：
  self_edit 是 LLM 语义级判重（更准），Jaccard 是零成本兜底。
  self_edit 先行；LLM 不可用或判定「全新」时回退到 Jaccard。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Optional

import requests

logger = logging.getLogger("mem0x.self_edit")

# ── 用户 ID ──
def _get_user_id() -> str:
    from .utils import get_user_id
    return get_user_id()

USER_ID = _get_user_id()
AGENT_ID = os.environ.get("MEM0_AGENT_ID", "hermes")

# ── 配置 ──
SELF_EDIT_ENABLED = os.environ.get("BO_MEM0_SELF_EDIT_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off",
}

_CANDIDATE_SIM_FLOOR = 0.25

_SELF_EDIT_SYSTEM = (
    "你是记忆去重引擎。判断「新记忆」与「既有候选」之间的关系，"
    "只输出一个 JSON 对象，不要输出任何解释。"
)

_SELF_EDIT_USER_TEMPLATE = """新记忆：
{new_text}

既有候选：
{candidates}

判断新记忆与候选的关系：
- "duplicate"：新记忆与某条候选是同一事实/偏好的重复，需要合并
- "conflict"：新记忆与某条候选矛盾（如偏好反转、状态变更），需要保留双方
- "distinct"：新记忆是全新内容，无需合并

输出 JSON：
{{"decision": "duplicate|conflict|distinct", "memory_id": "命中的候选id(duplicate/conflict必填)", "merged_content": "合并后的完整文本", "confidence": 0.0-1.0, "reason": "一句话说明"}}

要求：
1. merged_content 必须同时保留所有关键信息；conflict 时用「旧：... | 新：...」并标注时间
2. 只有高度确定才判 duplicate/conflict，不确定一律 distinct
3. 只输出 JSON 对象"""

# ── LLM 配置（从 config.json 读取） ──
_llm_config_cache: Optional[dict] = None
_llm_config_cached_at: float = 0.0
_llm_config_lock = threading.Lock()


def _get_llm_config() -> dict:
    """从 config.json 的 mem0.llm 段读取 LLM 配置。"""
    global _llm_config_cache, _llm_config_cached_at
    if _llm_config_cache is not None and time.time() - _llm_config_cached_at < 300:
        return _llm_config_cache

    with _llm_config_lock:
        if _llm_config_cache is not None and time.time() - _llm_config_cached_at < 300:
            return _llm_config_cache

        _llm_config_cache = None
        cfg = {"model": "", "base_url": "", "api_key": ""}
        try:
            from .utils import get_config
            raw = get_config()
            llm_cfg = raw.get("mem0", {}).get("llm", {}).get("config", {})
            cfg["model"] = llm_cfg.get("model", "")
            cfg["base_url"] = llm_cfg.get("openai_base_url", "")
            cfg["api_key"] = llm_cfg.get("api_key", "")
            if cfg["api_key"] and cfg["model"] and cfg["base_url"]:
                _llm_config_cache = cfg
                _llm_config_cached_at = time.time()
            else:
                logger.warning("config.json mem0.llm 配置不完整，self-edit LLM 不可用")
        except Exception as e:
            logger.warning(f"读取 LLM 配置失败: {e}")
        return cfg


# ── SQLite 账本 ──
def _get_db_path() -> str:
    from .utils import get_data_dir
    return os.path.join(get_data_dir(), "self_edit.db")

_schema_checked = False
_schema_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    db_path = _get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema() -> None:
    global _schema_checked
    with _schema_lock:
        if _schema_checked:
            return
        conn = _get_db()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_edits (
                    edit_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id   TEXT NOT NULL,
                    action      TEXT NOT NULL,
                    old_content TEXT NOT NULL,
                    new_content TEXT NOT NULL,
                    reason      TEXT DEFAULT '',
                    confidence  REAL DEFAULT 0.5,
                    undone      INTEGER DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_edits_memory ON memory_edits(memory_id)")
            conn.commit()
            _schema_checked = True
        except Exception as e:
            logger.warning(f"memory_edits 表初始化失败: {e}")
        finally:
            conn.close()


def _call_llm(prompt: str, system: str = "", max_tokens: int = 512, temperature: float = 0.2) -> Optional[str]:
    """直接调用 OpenAI 兼容 API，失败返回 None。"""
    cfg = _get_llm_config()
    if not cfg.get("api_key") or not cfg.get("model"):
        return None

    base = cfg["base_url"].rstrip("/")
    endpoint = f"{base}/chat/completions" if not base.endswith("/chat/completions") else base

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        r = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            json={"model": cfg["model"], "messages": messages, "max_tokens": max_tokens, "temperature": temperature},
            timeout=45,
        )
        if r.status_code == 200:
            choices = r.json().get("choices") or []
            if choices:
                return (choices[0].get("message") or {}).get("content", "").strip()
        else:
            logger.warning("LLM 调用失败: HTTP %d: %s", r.status_code, r.text[:200])
        return None
    except Exception as e:
        logger.debug(f"LLM 调用异常（降级）: {e}")
        return None


def _jaccard_sim(a: str, b: str) -> float:
    from .dedup import jaccard_sim
    return jaccard_sim(a, b)


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(m.get("content", "") for m in content if isinstance(m, dict)).strip()
    if isinstance(content, dict):
        return content.get("content", str(content)).strip()
    return str(content).strip()


def _search_candidates(memory, new_text: str, limit: int = 3, _pre_candidates: list = None) -> list[dict]:
    try:
        if _pre_candidates is not None:
            results = _pre_candidates
        else:
            try:
                raw = memory.search(new_text, filters={"user_id": USER_ID, "agent_id": AGENT_ID}, top_k=limit)
            except TypeError:
                raw = memory.search(new_text, filters={"user_id": USER_ID, "agent_id": AGENT_ID}, limit=limit)
            results = raw.get("results", raw) if isinstance(raw, dict) else raw
        if not isinstance(results, list):
            return []
        return [
            {
                "memory_id": r.get("id", ""),
                "content": (r.get("memory") or "").strip(),
                "score": r.get("score", 0) or 0,
            }
            for r in results
            if isinstance(r, dict) and (r.get("memory") or "").strip()
        ]
    except Exception as e:
        logger.debug(f"候选检索失败（降级）: {e}")
        return []


def _parse_decision(raw: str) -> Optional[dict]:
    if not raw:
        return None
    text = raw.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return None


def _detect_relation(memory, new_text: str, _pre_candidates: list = None) -> Optional[dict]:
    candidates = _search_candidates(memory, new_text, _pre_candidates=_pre_candidates)
    if not candidates:
        return None

    top_sim = max((_jaccard_sim(new_text, c["content"]) for c in candidates), default=0.0)
    if top_sim < _CANDIDATE_SIM_FLOOR:
        logger.debug("self-edit: 最相似候选 jaccard=%.2f < %.2f，跳过 LLM", top_sim, _CANDIDATE_SIM_FLOOR)
        return None

    cand_block = "\n".join(f"[id={c['memory_id']}] {c['content'][:200]}" for c in candidates)
    raw = _call_llm(
        _SELF_EDIT_USER_TEMPLATE.format(new_text=new_text[:400], candidates=cand_block),
        system=_SELF_EDIT_SYSTEM,
    )
    if not raw:
        return None
    decision = _parse_decision(raw)
    if not decision:
        return None

    verdict = str(decision.get("decision") or "").strip().lower()
    if verdict not in ("duplicate", "conflict"):
        return None

    memory_id = str(decision.get("memory_id") or "").strip()
    merged = str(decision.get("merged_content") or "").strip()
    if not memory_id or not merged:
        return None

    from .injection_guard import validate_memory_content
    is_safe, sanitized, rejection = validate_memory_content(merged)
    if not is_safe:
        logger.warning("🛡️ self-edit 合并结果含不安全指令，拦截: %s", rejection)
        return None
    merged = sanitized

    if memory_id not in {c["memory_id"] for c in candidates}:
        logger.warning("self-edit: LLM 返回候选之外的 memory_id=%s，降级为 distinct", memory_id)
        return None

    confidence = 0.5
    try:
        confidence = max(0.0, min(1.0, float(decision.get("confidence", 0.5))))
    except (TypeError, ValueError):
        pass

    return {
        "decision": verdict,
        "memory_id": memory_id,
        "merged_content": merged,
        "confidence": confidence,
        "reason": str(decision.get("reason") or ""),
    }


def _snapshot_old(memory, memory_id: str) -> str:
    try:
        got = memory.get(memory_id)
        if isinstance(got, dict):
            return str(got.get("memory") or got.get("content") or "").strip()
    except Exception as e:
        logger.debug(f"memory.get 快照失败: {e}")
    return ""


def _log_edit(memory_id: str, action: str, old_content: str, new_content: str, reason: str, confidence: float) -> int:
    _ensure_schema()
    conn = _get_db()
    try:
        cur = conn.execute(
            "INSERT INTO memory_edits (memory_id, action, old_content, new_content, reason, confidence) VALUES (?,?,?,?,?,?)",
            (memory_id, action, old_content, new_content, reason, confidence),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    except Exception as e:
        logger.warning(f"编辑账本写入失败: {e}")
        return 0
    finally:
        conn.close()


# ═══════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════

def self_edit_on_add(memory, content: Any, _pre_candidates: list = None) -> Optional[dict]:
    """写入前自编辑入口。

    返回 None → 无需合并，按正常流程新增。
    返回 dict → {action, memory_id, merged_content, edit_id, confidence}
    """
    if not SELF_EDIT_ENABLED or memory is None:
        return None

    new_text = _extract_text(content)
    if not new_text or len(new_text) < 10:
        return None

    try:
        relation = _detect_relation(memory, new_text, _pre_candidates=_pre_candidates)
    except Exception as e:
        logger.debug(f"self-edit 检测异常（降级）: {e}")
        return None

    if not relation:
        return None

    memory_id = relation["memory_id"]
    old_content = _snapshot_old(memory, memory_id)
    merged = relation["merged_content"]

    try:
        memory.update(memory_id, merged)
    except Exception as e:
        logger.warning(f"self-edit 合并更新失败（降级为新增）: {e}")
        return None

    try:
        edit_id = _log_edit(memory_id, relation["decision"], old_content, merged, relation["reason"], relation["confidence"])
        logger.info("✂️ self-edit: [%s] %s (edit_id=%d)", relation["decision"], memory_id[:8], edit_id)
    except Exception as e:
        edit_id = 0
        logger.warning(f"self-edit _log_edit 失败（主流程已完成）: {e}")

    return {
        "action": relation["decision"],
        "memory_id": memory_id,
        "merged_content": merged,
        "edit_id": edit_id,
        "confidence": relation["confidence"],
        "reason": relation["reason"],
    }


def rollback_edit(edit_id: int, memory=None) -> dict:
    """回滚一次自编辑。"""
    _ensure_schema()
    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM memory_edits WHERE edit_id=? AND undone=0", (edit_id,)).fetchone()
        if not row:
            return {"status": "error", "detail": f"edit_id={edit_id} 不存在或已回滚"}

        old_content = row["old_content"]
        memory_id = row["memory_id"]

        if not old_content:
            return {"status": "error", "detail": "该编辑无旧内容快照，无法回滚"}

        if memory is None:
            return {"status": "error", "detail": "mem0 实例未传入，无法回滚"}

        try:
            memory.update(memory_id, old_content)
        except Exception as e:
            return {"status": "error", "detail": f"恢复失败: {e}"}

        conn.execute("UPDATE memory_edits SET undone=1 WHERE edit_id=?", (edit_id,))
        conn.commit()

        logger.info("↩️ self-edit 回滚: edit_id=%d memory_id=%s", edit_id, memory_id)
        return {"status": "ok", "edit_id": edit_id, "memory_id": memory_id, "restored": old_content}
    finally:
        conn.close()


def list_edits(limit: int = 20, include_undone: bool = False) -> list[dict]:
    """列出编辑账本。"""
    _ensure_schema()
    conn = _get_db()
    try:
        sql = "SELECT * FROM memory_edits"
        if not include_undone:
            sql += " WHERE undone=0"
        sql += " ORDER BY edit_id DESC LIMIT ?"
        return [dict(r) for r in conn.execute(sql, (max(1, min(int(limit), 200)),)).fetchall()]
    finally:
        conn.close()
