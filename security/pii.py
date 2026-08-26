"""共享 PII 脱敏正则 + 工具函数。

统一定义：pipeline.py (服务端) + plugin/__init__.py (插件端) 共用。
"""
import re

# 18位身份证
ID_CARD_RE = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
)
# 11位手机号
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 邮箱
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
# 密码明文（中英文）
PASSWORD_RE = re.compile(
    r"(密码|口令|password|passwd|secret|token|api[_\-]?key)\s*[:：=]\s*\S+",
    re.IGNORECASE,
)

# plugin 端的替换规则（保持兼容）
PII_RULES = [
    (ID_CARD_RE, "[REDACTED_ID]"),
    (PHONE_RE, "[REDACTED_PHONE]"),
    (EMAIL_RE, "[REDACTED_EMAIL]"),
    (PASSWORD_RE, None),  # 特殊处理：保留键名
]


def redact_pii(text: str) -> str:
    """统一 PII 脱敏。"""
    if not text:
        return text
    result = text
    for pattern, replacement in PII_RULES:
        if replacement is not None:
            result = pattern.sub(replacement, result)
    # 密码：保留键名，值替换
    result = PASSWORD_RE.sub(r"\1=[REDACTED_PWD]", result)
    return result
