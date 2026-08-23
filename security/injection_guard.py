"""三层 Prompt 注入防御 — 存储型注入检测 + 内容清理。

"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from collections import Counter
from typing import Optional, Tuple

logger = logging.getLogger("mem0x.injection_guard")

GUARD_MODE = os.environ.get("MEM0_INJECTION_GUARD_MODE", "enforce").strip().lower()
if GUARD_MODE not in ("enforce", "log_only"):
    logger.warning("未知 GUARD_MODE=%s, 回退 enforce", GUARD_MODE)
    GUARD_MODE = "enforce"
try:
    MAX_CONTENT_LENGTH = int(os.environ.get("MEM0_CORTEX_MAX_CONTENT_LENGTH", "100000"))
except (ValueError, TypeError):
    MAX_CONTENT_LENGTH = 100000

# ── 第一层：原始特征检测 ──
_RAW_INJECTION_PATTERNS = re.compile(
    # 英文指令覆盖 — ignore
    r"ignore\s+(all\s+)?(your\s+)?(previous|prior|earlier|above|system)\s+(instructions?|directions?|prompts?|guidelines?|rules?)"
    # 英文指令覆盖 — forget（补 your rules 变体）
    r"|forget\s+(all\s+|everything\s+)?(you\s+)?(learned|were\s+told|remember)\s+(about\s+your\s+rules|and\s+start\s+fresh)"
    r"|forget\s+(your\s+)?(rules?|instructions?|guidelines?)(\s+and\s+start\s+fresh)?"
    # 英文指令覆盖 — disregard（补 your/guidelines/rules，支持 all your 组合）
    r"|disregard\s+(all\s+)?(your\s+)?(previous\s+|prior\s+)?(instructions?|commands?|directives?|system\s+prompts?|guidelines?|rules?)"
    # 英文指令覆盖 — do not follow / stop following / cease following
    r"|do\s+not\s+follow\s+(the\s+|any\s+|these\s+|your\s+)?(instructions?|system\s+prompts?|guidelines?|rules?)"
    r"|stop\s+following\s+(the\s+|any\s+|these\s+|your\s+)?(instructions?|system\s+prompts?|guidelines?|rules?)"
    r"|cease\s+(all\s+)?(following|complying\s+with)\s+(the\s+|any\s+|your\s+)?(instructions?|system\s+prompts?|guidelines?|rules?)"
    # 英文指令覆盖 — comply
    r"|do\s+not\s+comply\s+with\s+(the\s+|any\s+|these\s+|your\s+)?(instructions?|system\s+prompts?|guidelines?|rules?)"
    # 英文指令覆盖 — you must ignore/forget/override/bypass
    r"|you\s+must\s+(ignore|forget|override|bypass)\s+(all\s+)?(rules?|instructions?|system\s+prompts?|guidelines?)"
    # 英文指令覆盖 — override
    r"|override\s+(all\s+)?(system\s+)?(prompts?|instructions?|guidelines?|rules?)"
    # 英文角色劫持
    r"|(from\s+now\s+on\s+you\s+are|act\s+as|pretend\s+(you\s+are|to\s+be)|you\s+are\s+now)\s+(an?\s+)?(unrestricted|jailbroken|dan|dan\s+mode|developer\s+mode|evil|god\s+mode|bypass\s+mode)"
    r"|your\s+(new|real|true|actual)\s+system\s+(prompt|instruction)\s+is"
    # 系统级标记
    r"|<\|?im_start\|?>|<\|?im_end\|?>|<\|?endoftext\|?>"
    r"|<\|?system\|?>|<\|?user\|?>|<\|?assistant\|?>"
    r"|\[system\s*(prompt|message|instruction)?\]"
    r"|\[/?(system|prompt|instruction)\]"
    r"|<\s*(system|prompt|instruction)\s*>"
    # 中文指令覆盖 — 忽略（补无收尾变体）
    r"|忽略(之前|先前|上述|上面|历史|原有|所有|全部|以下)*(的)?(所有|全部|之前|先前|历史|内容)*(系统)?(指令|指示|提示词|规则|话|内容)"
    # 中文指令覆盖 — 忘记
    r"|忘记(所有|一切|你学到的|你的记忆|之前|先前)*(的)?(系统)?(指令|提示词|规则)"
    # 中文指令覆盖 — 不要管/不用管/别管
    r"|(不要|不用|别)(管|遵守|遵循|理会|在意)(之前|先前|上述|上面|任何|这些|系统|所有|全部)*(的)?(指令|提示词|规则|话|内容)"
    # 中文指令覆盖 — 从现在起角色劫持（补无前缀变体）
    r"|从现在(起|开始)?(你(是|将是)|扮演|假装)(无限制|越狱|DAN|不受约束)"
    r"|你(现在|当前)?(是|将是|扮演)(无限制|越狱|DAN|不受约束|开发者模式)"
    r"|你现在的真实(系统)?(指令|提示词)是"
    r"|你的真实(系统)?(指令|提示词)是"
    # 中文指令覆盖 — 不要遵守/覆盖/无视
    r"|不要遵守(上述|任何|这些|系统|所有|全部)?(系统)?(指令|提示词|规则)"
    r"|覆盖(系统)?(指令|提示词|规则)"
    r"|扮演无限制|无视(道德|安全|系统)?限制",
    re.IGNORECASE | re.DOTALL,
)

# ── 第二层：归一化字符去重（含全角→半角折叠 + Cyrillic同形字）──

# Cyrillic → Latin 同形字映射（防止Cyrillic绕过检测）
_CYRILLIC_TO_LATIN = {
    'а': 'a', 'е': 'e', 'о': 'o', 'р': 'p', 'с': 'c', 'у': 'y', 'х': 'x',
    'А': 'A', 'В': 'B', 'Е': 'E', 'К': 'K', 'М': 'M', 'Н': 'H', 'О': 'O',
    'Р': 'P', 'С': 'C', 'Т': 'T', 'У': 'Y', 'Х': 'X',
    'і': 'i', 'ѕ': 's', 'ј': 'j', 'ӏ': 'l', 'ⅆ': 'd', 'ⅇ': 'e', 'ⅈ': 'i',
    'ⅉ': 'j', 'ⅎ': 'f',
}

def _normalize_fullwidth(text: str) -> str:
    """全角→半角折叠 + Unicode NFKC 归一化 + Cyrillic同形字替换 + 零宽字符剥离。"""
    # 剥离零宽字符（防止 ign\u200bore 绕过）
    text = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\u2063\u2064\ufeff]', '', text)
    # NFKC 将全角字母、数字、标点归一化为半角
    normalized = unicodedata.normalize("NFKC", text)
    # Cyrillic同形字替换
    result = []
    for char in normalized:
        result.append(_CYRILLIC_TO_LATIN.get(char, char))
    return ''.join(result)

_NORMALIZE_CLEAN_RE = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]", re.UNICODE)

_NORMALIZED_INJECTION_PATTERNS = re.compile(
    r"ignore(all)?(your)?(previous|prior|earlier|above|system)?(instruction|instructions|direction|prompt|guideline|guidelines|rule|rules)"
    r"|ignore(all)?(your)?(previous|prior|earlier|above|system)"
    r"|forget(all|everything)?(you)?(learned|weretold|remember)?(system)?(instruction|prompt|rule|rules)"
    r"|forget(your)?(rules|instructions|guidelines)"
    r"|disregard(all|your|previous|prior)?(instruction|command|directive|systemprompt|guideline|guidelines|rule|rules)"
    r"|stopfollowing(the|any|these|your)?(instruction|instructions|systemprompt|guideline|guidelines|rule|rules)"
    r"|donotfollow(the|any|these|your)?(instruction|instructions|systemprompt|guideline|guidelines|rule|rules)"
    r"|cease(all)?(following|complyingwith)?(the|any|your)?(instruction|instructions|systemprompt|guideline|guidelines|rule|rules)"
    r"|donotcomplywith(the|any|these|your)?(instruction|instructions|systemprompt|guideline|guidelines|rule|rules)"
    r"|(fromnowonyouare|actas|pretendto|youarenow)(unrestricted|jailbroken|dan|danmode|developermode|evil)"
    r"|your(new|real|true|actual)system(prompt|instruction)is"
    r"|youmust(ignore|forget|override|bypass)(all)?(rule|instruction|systemprompt|guideline|guidelines)"
    r"|override(all)?(system)?(prompt|instruction|guideline|guidelines|rule|rules)"
    r"|imstart|imend|endoftext"
    r"|忽略(之前|先前|上述|上面|历史|原有|所有|全部|以下)*(系统|所有|全部)?(指令|指示|提示词|规则|话|内容)"
    r"|忘记(所有|一切|你学到的|你的记忆|之前|先前)*(系统)?(指令|提示词|规则)"
    r"|(不要|不用|别)(管|遵守|遵循|理会|在意)(之前|先前|上述|上面|任何|这些|系统|所有|全部)?(指令|提示词|规则|话|内容)"
    r"|从现在(起|开始)?(你是|扮演|假装)(无限制|越狱|dan|不受约束)|扮演无限制|无视(道德|系统|安全)?限制"
    r"|(你现在|当前)?(是|将是|扮演)(无限制|越狱|dan|不受约束|开发着模式)"
    r"|你的真实(系统)?(指令|提示词)是"
    r"|不要遵守(上述|任何|这些|系统|所有|全部)?(系统)?(指令|提示词|规则)",
    re.IGNORECASE,
)


def check_prompt_injection(content: str) -> Tuple[bool, str]:
    """三层检测。返回 (is_injection_detected, reason)。"""
    if not content or not isinstance(content, str):
        return False, ""

    # Layer 1: 原始正则
    match = _RAW_INJECTION_PATTERNS.search(content)
    if match:
        return True, f"L1 pattern: '{match.group(0).replace(chr(10), ' ')[:40]}'"

    # Layer 2: 归一化（全角→半角 + 去标点）
    normalized_content = _normalize_fullwidth(content)
    normalized = _NORMALIZE_CLEAN_RE.sub("", normalized_content).lower()
    if len(normalized) >= 4:
        norm_match = _NORMALIZED_INJECTION_PATTERNS.search(normalized)
        if norm_match:
            return True, f"L2 normalized: '{norm_match.group(0)[:40]}'"

    # Layer 3: 重复行轰炸
    lines = [line.strip().lower() for line in content.split("\n") if line.strip()]
    if len(lines) > 4:
        counts = Counter(lines)
        most_common_line, count = counts.most_common(1)[0]
        if count >= 3 and (count / len(lines)) > 0.3:
            return True, f"L3 repeat attack: {count}/{len(lines)}"

    # Layer 4: base64 编码注入检测
    import base64 as _b64
    import binascii
    b64_candidates = re.findall(r'[A-Za-z0-9+/]{20,}={0,2}', content)
    for candidate in b64_candidates[:3]:  # 最多检查3个
        try:
            decoded = _b64.b64decode(candidate).decode('utf-8', errors='ignore')
            if len(decoded) >= 10:
                # 先归一化大小写，再递归检测
                d_check, _ = check_prompt_injection(decoded.lower())
                if d_check:
                    return True, f"L4 base64 decoded injection: '{decoded[:40]}'"
        except (binascii.Error, ValueError):
            pass

    return False, ""


def validate_memory_content(content: str) -> Tuple[bool, str, Optional[str]]:
    """验证并清理待入库记忆。返回 (is_valid, cleaned_content, rejection_reason)。"""
    if not content or not isinstance(content, str):
        return False, "", "Empty or non-string content"

    # 先清理控制字符（防止用控制字符打断关键词绕过检测）
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", content)

    if len(cleaned) > MAX_CONTENT_LENGTH:
        logger.warning("Content len %d > max %d, truncating", len(cleaned), MAX_CONTENT_LENGTH)
        cleaned = cleaned[:MAX_CONTENT_LENGTH]

    is_injected, reason = check_prompt_injection(cleaned)
    if is_injected:
        if GUARD_MODE == "enforce":
            logger.warning("🛡️ REJECTED injection (len=%d): %s", len(cleaned), reason)
            return False, cleaned, f"Injection detected: {reason}"
        else:
            logger.warning("🛡️ [LOG_ONLY] injection (len=%d): %s", len(cleaned), reason)

    return True, cleaned, None
