"""spaCy NER 实体提取器 v2 — 提取领域实体（10 标签体系），存入 Qdrant payload tags。

标签体系：TECH, TOOL, PROJECT, CONFIG, PERSON, TIME, VERSION, FILE_PATH, COMMAND, ERROR
使用自训练模型（v* 目录），不再依赖 spaCy 预训练基础模型。
"""

import json
import logging
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger("spacy_ner")

# ── 从 ner_terms.json 加载配置（带缓存）──

_TERMS_CACHE = None

def _load_terms() -> dict:
    global _TERMS_CACHE
    if _TERMS_CACHE is not None:
        return _TERMS_CACHE
    terms_path = Path(__file__).parent / "ner_terms.json"
    if terms_path.exists():
        with open(terms_path, "r", encoding="utf-8") as f:
            _TERMS_CACHE = json.load(f)
    else:
        logger.warning("ner_terms.json not found, using empty terms")
        _TERMS_CACHE = {}
    return _TERMS_CACHE


def _get_stop_entities():
    return frozenset(_load_terms().get("stop_entities", []))

def _get_tech_terms():
    return frozenset(_load_terms().get("tech_terms", []))

def _get_tool_terms():
    return frozenset(_load_terms().get("tool_terms", []))

def _get_project_terms():
    return frozenset(_load_terms().get("project_terms", []))

def _get_person_terms():
    return frozenset(_load_terms().get("person_terms", []))

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

# CONFIG 正则
_CONFIG_PATTERNS = [
    re.compile(r"config\.yaml|config\.yml|config\.json"),
    re.compile(r"MEM0X_HOME|MEM0X_DATA_DIR|MEM0X_CONFIG"),
    re.compile(r"docker-compose\.yml"),
    re.compile(r"gateway\.pid"),
]


def _is_ascii_alnum(ch: str) -> bool:
    """Check if character is ASCII alphanumeric only (not CJK, etc.)."""
    return ch.isascii() and ch.isalnum()


def _extract_domain_entities(content: str) -> list[dict[str, str]]:
    """用领域字典+正则提取实体（增强 spaCy 输出）。
    
    标签优先级：COMMAND > VERSION > TIME > ERROR > TECH > TOOL > PROJECT > CONFIG > PERSON
    """
    results = []
    seen = set()  # (start, end) 避免重叠

    def _add(start, end, label):
        span = (start, end)
        if span not in seen and start < end and len(content[start:end].strip()) >= 2:
            seen.add(span)
            text = content[start:end].strip()
            if text not in seen:
                seen.add(text)
                results.append({"text": text, "label": label})

    # 1. COMMAND（最高优先级）
    for m in _CMD_PATTERN.finditer(content):
        _add(m.start(), m.end(), "COMMAND")

    # 2. VERSION
    for m in _VERSION_PATTERN.finditer(content):
        _add(m.start(), m.end(), "VERSION")

    # 3. TIME
    for pat in _TIME_PATTERNS:
        for m in pat.finditer(content):
            _add(m.start(), m.end(), "TIME")

    # 4. ERROR
    for pat in _ERROR_PATTERNS:
        for m in pat.finditer(content):
            _add(m.start(), m.end(), "ERROR")

    # 5. TECH
    for term in _get_tech_terms():
        if len(term) < 2:
            continue
        start = 0
        while True:
            idx = content.find(term, start)
            if idx < 0:
                break
            end = idx + len(term)
            before_ok = idx == 0 or not _is_ascii_alnum(content[idx - 1])
            after_ok = end == len(content) or not _is_ascii_alnum(content[end])
            if before_ok and after_ok:
                _add(idx, end, "TECH")
            start = idx + 1

    # 6. TOOL
    for term in _get_tool_terms():
        if len(term) < 2:
            continue
        start = 0
        while True:
            idx = content.find(term, start)
            if idx < 0:
                break
            end = idx + len(term)
            before_ok = idx == 0 or not _is_ascii_alnum(content[idx - 1])
            after_ok = end == len(content) or not _is_ascii_alnum(content[end])
            if before_ok and after_ok:
                _add(idx, end, "TOOL")
            start = idx + 1

    # 7. PROJECT
    for term in _get_project_terms():
        if len(term) < 2:
            continue
        start = 0
        while True:
            idx = content.find(term, start)
            if idx < 0:
                break
            end = idx + len(term)
            before_ok = idx == 0 or not _is_ascii_alnum(content[idx - 1])
            after_ok = end == len(content) or not _is_ascii_alnum(content[end])
            if before_ok and after_ok:
                _add(idx, end, "PROJECT")
            start = idx + 1

    # 8. CONFIG
    for pat in _CONFIG_PATTERNS:
        for m in pat.finditer(content):
            _add(m.start(), m.end(), "CONFIG")

    # 9. PERSON（仅中文人名后缀）
    _PERSON_SUFFIXES = ("老师", "先生", "女士", "博士", "教授")
    for suffix in _PERSON_SUFFIXES:
        for m in re.finditer(r'[\u4e00-\u9fff]{1,6}' + re.escape(suffix), content):
            _add(m.start(), m.end(), "PERSON")
    for term in _get_person_terms():
        start = 0
        while True:
            idx = content.find(term, start)
            if idx < 0:
                break
            end = idx + len(term)
            _add(idx, end, "PERSON")
            start = idx + 1

    return results


_nlp = None
_model_lock = threading.Lock()
_current_model_path: str | None = None


def reload_model() -> bool:
    """扫描最新训练模型，如果有新版本则热加载替换全局 _nlp。线程安全。"""
    global _nlp, _current_model_path
    import spacy
    import glob as _glob

    data_dir = os.environ.get("MEM0X_DATA_DIR", "/app/data")
    model_dir = os.path.join(data_dir, "ner_models")

    with _model_lock:
        # 找到最新版本模型路径
        latest_path = None
        if os.path.isdir(model_dir):
            versions = sorted(_glob.glob(os.path.join(model_dir, "v*")), reverse=True)
            if versions:
                latest_path = versions[0]

        # 比较当前路径与最新路径
        if latest_path == _current_model_path:
            return False

        # 加载新模型
        if latest_path:
            try:
                new_nlp = spacy.load(latest_path)
                _nlp = new_nlp
                _current_model_path = latest_path
                logger.info("reload_model: 热加载成功 -> %s", latest_path)
                return True
            except Exception as e:
                logger.warning("reload_model: 加载失败 %s: %s", latest_path, e)
                return False
        else:
            # 无训练模型，无法运行 NER
            logger.error("reload_model: 无可用训练模型，NER 不可用")
            return False


def _get_nlp():
    global _nlp, _current_model_path
    if _nlp is None:
        import spacy
        import glob

        # 优先加载训练好的模型
        data_dir = os.environ.get("MEM0X_DATA_DIR", "/app/data")
        model_dir = os.path.join(data_dir, "ner_models")
        if os.path.isdir(model_dir):
            versions = sorted(glob.glob(os.path.join(model_dir, "v*")), reverse=True)
            for vpath in versions:
                try:
                    _nlp = spacy.load(vpath)
                    _current_model_path = vpath
                    logger.info("训练模型加载成功: %s (labels=%s)", vpath, list(_nlp.get_pipe("ner").labels))
                    return _nlp if _nlp is not False else None
                except Exception:
                    continue

        # 无训练模型，NER 不可用
        logger.error("NER 训练模型未找到，extract_tags 将返回空结果")
        _nlp = False
    return _nlp if _nlp is not False else None


def extract_tags(content: str, top_n: int = 10) -> list[str]:
    """从文本中提取实体标签，去重+按频率排序。结合 spaCy + 领域字典。"""
    entities = []

    # 领域字典提取
    domain_ents = _extract_domain_entities(content)
    entities.extend([e["text"] for e in domain_ents])

    # spaCy 提取（补充）
    nlp = _get_nlp()
    if nlp is not None:
        try:
            doc = nlp(content[:10000])
            for ent in doc.ents:
                t = ent.text.strip()
                if len(t) >= 2 and t.lower() not in _get_stop_entities():
                    entities.append(t)
        except Exception as e:
            logger.debug("spaCy NER 失败: %s", e)

    counter = Counter(entities)
    return [word for word, _ in counter.most_common(top_n)]


def extract_tags_with_types(content: str, top_n: int = 10) -> list[dict[str, str]]:
    """提取实体并返回 text + label（供训练缓冲区使用）。
    优先使用领域字典标签，spaCy 仅作补充。
    """
    results = []
    seen = set()

    # 领域字典提取（有标签）
    domain_ents = _extract_domain_entities(content)
    for e in domain_ents:
        if e["text"] not in seen:
            seen.add(e["text"])
            results.append(e)

    # spaCy 提取（补充，标签可能不准确）
    nlp = _get_nlp()
    if nlp is not None:
        try:
            doc = nlp(content[:10000])
            for ent in doc.ents:
                t = ent.text.strip()
                if len(t) < 2 or t.lower() in _get_stop_entities() or t in seen:
                    continue
                seen.add(t)
                # spaCy 标签映射到领域标签
                label = _map_spacy_label(ent.label_)
                results.append({"text": t, "label": label})
        except Exception as e:
            logger.debug("spaCy NER (with types) 失败: %s", e)

    return results[:top_n]


def _map_spacy_label(spacy_label: str) -> str:
    """返回所有 NER 标签，不做白名单过滤。"""
    return spacy_label


# ── 模型热加载 watcher ──

_watcher_thread: threading.Thread | None = None
_watcher_running = False
_watcher_last_attempted: str | None = None  # 上次尝试的 version，避免重复触发


def start_model_watcher(interval: int = 30) -> None:
    """启动后台线程，定期检查 latest.json 并热加载新模型。"""
    global _watcher_thread, _watcher_running
    if _watcher_running:
        return
    _watcher_running = True
    _watcher_thread = threading.Thread(
        target=_watcher_loop,
        args=(interval,),
        daemon=True,
        name="ner_model_watcher",
    )
    _watcher_thread.start()
    logger.info("model_watcher 已启动 (interval=%ds)", interval)


def stop_model_watcher() -> None:
    """停止模型 watcher 线程。"""
    global _watcher_running, _watcher_thread
    _watcher_running = False
    if _watcher_thread and _watcher_thread.is_alive():
        _watcher_thread.join(timeout=5)
    _watcher_thread = None
    logger.info("model_watcher 已停止")


def _watcher_loop(interval: int) -> None:
    """后台循环：检查 latest.json 版本变化，触发热加载。"""
    global _watcher_running, _watcher_last_attempted
    while _watcher_running:
        try:
            latest = _read_latest_json()
            if latest:
                version = latest.get("version", "")
                if version:
                    # 检查版本是否与当前加载的不同
                    current = _current_model_path or ""
                    if not current.endswith(version):
                        # 避免同一失败版本反复触发
                        if version != _watcher_last_attempted:
                            logger.info("model_watcher: 检测到新模型 version=%s，触发热加载", version)
                            _watcher_last_attempted = version
                            reload_model()
                        # version == _last_attempted: 已尝试过，跳过
                    else:
                        # 当前已加载该版本，重置 attempted
                        _watcher_last_attempted = None
        except Exception as e:
            logger.warning("model_watcher 异常: %s", e)
        time.sleep(interval)


def _read_latest_json() -> dict | None:
    """读取 data/ner_models/latest.json。"""
    data_dir = os.environ.get("MEM0X_DATA_DIR", "/app/data")
    latest_path = os.path.join(data_dir, "ner_models", "latest.json")
    if not os.path.exists(latest_path):
        return None
    try:
        with open(latest_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# 模块加载时自动启动 watcher
start_model_watcher()


def is_available() -> bool:
    return _get_nlp() is not None
