"""
mem0 运行时：单例管理 + 配置加载 + rerank

"""
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests as req

logger = logging.getLogger("mem0x.runtime")

# ── 全局状态 ──────────────────────────────────────────────────
_memory_instance = None
_mem_init_lock = threading.Lock()
_config_cache: Optional[Dict] = None
_rerank_config_cache: Optional[Dict] = None

# ── 路径配置 ──────────────────────────────────────────────────
# 优先级：环境变量 > ~/.mem0x/ > 项目目录
HOME_DIR = Path.home()
MEM0X_HOME = Path(os.environ.get("MEM0X_HOME", str(HOME_DIR / ".mem0x")))
PROJECT_DIR = Path(__file__).resolve().parent.parent


# ── 配置加载 ──────────────────────────────────────────────────
def load_config(config_path: Optional[str] = None) -> Dict:
    """加载配置文件，支持环境变量覆盖路径。

    查找顺序：
    1. 环境变量 MEM0X_CONFIG
    2. ~/.mem0x/config.json
    3. 项目目录/config.json
    """
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    if config_path is None:
        config_path = os.environ.get("MEM0X_CONFIG")

    if config_path is None:
        # 按优先级查找
        candidates = [
            MEM0X_HOME / "config.json",
            PROJECT_DIR / "config.json",
        ]
        for p in candidates:
            if p.exists():
                config_path = str(p)
                break

    if config_path is None:
        raise FileNotFoundError("找不到 config.json，已尝试: ~/.mem0x/config.json, 项目目录/config.json")

    with open(config_path, "r", encoding="utf") as f:
        _config_cache = json.load(f)

    # 环境变量覆盖敏感信息
    _override_from_env(_config_cache)
    logger.info("配置已加载: %s", config_path)
    return _config_cache


def _override_from_env(cfg: Dict):
    """从环境变量覆盖 API key 等敏感配置。"""
    env_map = {
        "BO_MEM0_LLM_API_KEY": ("mem0", "llm", "config", "api_key"),
        "BO_MEM0_EMBEDDER_API_KEY": ("mem0", "embedder", "config", "api_key"),
        "BO_MEM0_QDRANT_API_KEY": ("mem0", "vector_store", "config", "api_key"),
        "BO_MEM0_RERANK_API_KEY": ("rerank", "config", "api_key"),
        "BO_MEM0_NEO4J_PASSWORD": ("neo4j", "password"),
    }
    for env_key, path in env_map.items():
        val = os.environ.get(env_key)
        if val:
            obj = cfg
            for k in path[:-1]:
                obj = obj.setdefault(k, {})
            obj[path[-1]] = val


def reset_config_cache():
    """重置配置缓存（热更新用）。"""
    global _config_cache, _rerank_config_cache
    _config_cache = None
    _rerank_config_cache = None


# ── mem0 单例 ─────────────────────────────────────────────────
def get_memory(config: Optional[Dict] = None):
    """获取 mem0 AsyncMemory 单例（懒加载 + 双重锁）。"""
    global _memory_instance
    if _memory_instance is not None:
        return _memory_instance

    with _mem_init_lock:
        if _memory_instance is not None:
            return _memory_instance

        if config is None:
            config = load_config()

        mem0_cfg = config.get("mem0", {})
        from mem0 import AsyncMemory

        _memory_instance = AsyncMemory.from_config(mem0_cfg)
        logger.info("mem0 AsyncMemory 单例初始化完成")
        return _memory_instance


def reset_memory_singleton():
    """重置 mem0 单例（配置变更后调用）。"""
    global _memory_instance
    with _mem_init_lock:
        _memory_instance = None
    reset_config_cache()
    logger.info("mem0 单例已重置")


# ── Rerank ────────────────────────────────────────────────────
def _load_rerank_config(config: Optional[Dict] = None) -> Optional[Dict]:
    """加载 rerank 配置。"""
    global _rerank_config_cache
    if _rerank_config_cache is not None:
        return _rerank_config_cache

    if config is None:
        config = load_config()

    rc = config.get("rerank")
    if not rc or not rc.get("config", {}).get("api_key"):
        return None

    _rerank_config_cache = rc
    return _rerank_config_cache


def rerank(
    query: str,
    documents: List[str],
    top_n: int = 10,
    config: Optional[Dict] = None,
) -> List[Dict]:
    """
    统一 rerank 入口。返回 [{"index": i, "relevance_score": s}, ...]
    失败返回空列表（不阻断主链路）。
    """
    rc = _load_rerank_config(config)
    if rc is None:
        return []

    provider = rc.get("provider", "siliconflow").lower()
    try:
        if provider in ("siliconflow", "sf", "openai_compatible", "openai"):
            return _rerank_openai_compatible(query, documents, top_n, rc)
        elif provider == "jina":
            return _rerank_jina(query, documents, top_n, rc)
        elif provider == "cohere":
            return _rerank_cohere(query, documents, top_n, rc)
        else:
            logger.warning("未知 rerank provider: %s", provider)
            return []
    except Exception as e:
        logger.warning("rerank 失败: %s", e)
        return []


def _rerank_openai_compatible(
    query: str, documents: List[str], top_n: int, rc: Dict
) -> List[Dict]:
    """OpenAI 兼容 rerank（SiliconFlow / vLLM / LiteLLM）。"""
    cfg = rc["config"]
    url = cfg.get("openai_base_url", "https://api.siliconflow.cn/v1").rstrip("/")
    url = f"{url}/rerank"

    payload = {
        "model": cfg["model"],
        "query": query,
        "documents": documents,
        "top_n": min(top_n, len(documents)),
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }

    resp = req.post(url, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("results", []):
        results.append({
            "index": item.get("index", 0),
            "relevance_score": item.get("relevance_score", 0.0),
        })
    return results


def _rerank_jina(
    query: str, documents: List[str], top_n: int, rc: Dict
) -> List[Dict]:
    """Jina AI rerank。"""
    cfg = rc["config"]
    url = "https://api.jina.ai/v1/rerank"

    payload = {
        "model": cfg.get("model", "jina-reranker-v2-base-multilingual"),
        "query": query,
        "documents": documents,
        "top_n": min(top_n, len(documents)),
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }

    resp = req.post(url, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("results", []):
        results.append({
            "index": item.get("index", 0),
            "relevance_score": item.get("relevance_score", 0.0),
        })
    return results


def _rerank_cohere(
    query: str, documents: List[str], top_n: int, rc: Dict
) -> List[Dict]:
    """Cohere rerank。"""
    cfg = rc["config"]
    url = "https://api.cohere.ai/v1/rerank"

    payload = {
        "model": cfg.get("model", "rerank-multilingual-v3.0"),
        "query": query,
        "documents": documents,
        "top_n": min(top_n, len(documents)),
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }

    resp = req.post(url, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("results", []):
        results.append({
            "index": item.get("index", 0),
            "relevance_score": item.get("relevance_score", 0.0),
        })
    return results
