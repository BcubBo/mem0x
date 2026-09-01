"""NER 弱监督标注器 v2 — 新标签体系。

标签体系（10 个标签）：
  TECH     — 编程语言、框架、协议、技术栈
  TOOL     — 软件工具、命令行工具
  PROJECT  — 项目名称
  CONFIG   — 配置文件、配置项
  PERSON   — 真实人名（仅限中文人名后缀：老师、先生、女士、博士）
  TIME     — 时间日期（2026-08-30、每日、每周）
  VERSION  — 版本号（v1.7.0、v0.1.27、3.8.0）
  FILE_PATH — 文件路径（quotes.py、SKILL.md、/app/data/）
  COMMAND  — 命令行操作（docker build、git pull、pip install）
  ERROR    — 错误异常（Connection refused、OOM、timeout）
"""

import logging
import re
from typing import Any

logger = logging.getLogger("ner_labeler")

# ── 有效标签集（10个领域标签，旧标签已移除）──
VALID_LABELS = frozenset({
    "TECH", "TOOL", "PROJECT", "CONFIG", "PERSON",
    "TIME", "VERSION", "FILE_PATH", "COMMAND", "ERROR",
})

# ── 规则修正层 ──

# 中文人名常见后缀
_PERSON_SUFFIXES = ("老师", "先生", "女士", "博士", "教授", "总", "工")

# 项目/产品常见模式
_PROJECT_PATTERNS = [
    re.compile(r"^[A-Z][a-zA-Z]+(?:API|DB|SDK|CLI)$"),   # Mem0xAPI
    re.compile(r"[\u4e00-\u9fff]{2,}(?:记|版)$"),          # 伏魔记
]

# FILE_PATH 正则
_FILE_EXTS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".vue", ".html", ".css",
    ".json", ".yaml", ".yml", ".toml", ".xml", ".md", ".txt",
    ".sh", ".bash", ".conf", ".cfg", ".ini", ".sql", ".go", ".rs",
})

_FILE_PATH_PATTERN = re.compile(
    r'(?:/[\w.-]+)+/[\w.-]+(?:' + '|'.join(re.escape(e) for e in _FILE_EXTS) + r')\b'
)

# VERSION 正则
_VERSION_PATTERN = re.compile(
    r'[vV]?(\d+\.\d+(?:\.\d+)?(?:[-.]?(?:alpha|beta|rc|dev|pre)\d*)?)\b'
)

# TIME 正则
_TIME_PATTERNS = [
    re.compile(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?'),
    re.compile(r'(?:每日|每周|每月|每年|daily|weekly|monthly|yearly)'),
]

# ERROR 正则
_ERROR_PATTERNS = [
    re.compile(r'Connection\s+refused', re.I),
    re.compile(r'OOM|Out\s+of\s+memory', re.I),
    re.compile(r'timeout|Timeout', re.I),
    re.compile(r'\berror\b|\bError\b|\bERROR\b'),
    re.compile(r'\bexception\b|\bException\b|\bEXCEPTION\b'),
    re.compile(r'\bfailed\b|\bFailed\b|\bFAILED\b'),
    re.compile(r'\bcrash\b|\bCrash\b|\bCRASH\b'),
]

# COMMAND 正则
_CMD_VERBS = frozenset({
    "docker", "git", "pip", "npm", "yarn", "pnpm", "uv", "conda",
    "curl", "wget", "sed", "awk", "grep", "find", "ls", "cd", "cp",
    "mv", "rm", "mkdir", "chmod", "chown", "ssh", "scp", "rsync",
    "cmake", "make", "gradle", "maven", "pytest", "python", "node",
    "go", "cargo", "rustc", "gcc", "g++", "clang",
    "systemctl", "journalctl", "tmux", "screen", "kill", "ps",
    "docker-compose", "kubectl", "helm", "terraform", "ansible",
    "psql", "sqlite3", "mysql", "mongo", "redis-cli", "pip3", "pipx",
})

_CMD_ACTIONS = frozenset({
    "build", "run", "pull", "push", "install", "uninstall",
    "start", "stop", "restart", "enable", "disable",
    "create", "delete", "remove", "update", "upgrade",
    "list", "show", "get", "set", "put", "exec", "attach",
    "logs", "inspect", "status", "info", "config", "init",
    "test", "lint", "format", "compile", "deploy",
    "add", "commit", "fetch", "merge", "rebase",
    "clone", "checkout", "branch", "tag", "stash",
    "search", "download", "upload", "serve", "watch",
})

_CMD_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(v) for v in _CMD_VERBS) + r')\s+(' +
    '|'.join(re.escape(a) for a in _CMD_ACTIONS) + r')\b'
)


def _rule_override(text: str, ent_text: str, ent_label: str) -> str:
    """基于规则修正实体标签。返回修正后的 label。"""
    t = ent_text.strip()

    # 如果已经是有效领域标签，直接返回
    if ent_label in VALID_LABELS:
        return ent_label

    # 人名后缀修正
    if t.endswith(_PERSON_SUFFIXES) and len(t) <= 8:
        return "PERSON"

    # 项目模式修正
    for pat in _PROJECT_PATTERNS:
        if pat.search(t):
            return "PROJECT"

    # 未识别的标签映射到 TECH（替代原来的 CONCEPT）
    return "TECH"


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

    entities.sort(key=lambda e: (e["start"], -e["end"]))
    result = []
    for ent in entities:
        if not result:
            result.append(ent)
            continue
        prev = result[-1]
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
        entities: [{"text": "Qdrant", "label": "TECH"}, ...]
    输出（spaCy training 格式）或 None：
        {"text": "...", "entities": [(start, end, "LABEL"), ...]}
    """
    if not text or not entities:
        return None

    labeled = []
    for ent in entities:
        ent_text = ent.get("text", "").strip()
        ent_label = ent.get("label", "TECH")

        if not _is_valid_entity(text, ent_text, ent_label):
            continue

        # 规则修正
        ent_label = _rule_override(text, ent_text, ent_label)

        # 在原文中定位
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
