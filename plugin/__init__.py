"""mem0x — Hermes MemoryProvider 插件

通过 HTTP 调用 mem0x 独立服务。
只用标准库 urllib，不给宿主装依赖。

部署：~/.hermes/profiles/bo/plugins/mem0x/
配置：memory.provider: mem0x
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes_plugins.mem0x")


# ═══════════════════════════════════════════════════
# HTTP 客户端（零依赖）
# ═══════════════════════════════════════════════════

class _Client:
    """轻量 HTTP 客户端（urllib，零依赖）。"""

    def __init__(self, base_url: str, api_key: str = ""):
        self.base = base_url.rstrip("/")
        self.api_key = api_key

    def request(self, method: str, path: str, body: Any = None, timeout: float = 6.0) -> Any:
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"} if data else {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def try_request(self, method: str, path: str, **kwargs) -> Optional[Any]:
        """失败返回 None，不让对话崩。"""
        try:
            return self.request(method, path, **kwargs)
        except Exception as e:
            logger.debug("mem0x request failed: %s", e)
            return None


_client: Optional[_Client] = None
_config: Optional[dict] = None


def _load_config() -> dict:
    """从 mem0x.json 加载配置。"""
    global _config
    if _config is not None:
        return _config
    try:
        config_path = os.path.join(
            os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
            "mem0x.json",
        )
        with open(config_path) as f:
            _config = json.load(f)
    except Exception as e:
        logger.debug("mem0x: failed to load config: %s, using defaults", e)
        _config = {}
    return _config


def _get_client() -> _Client:
    global _client
    if _client is None:
        cfg = _load_config()
        url = cfg.get("service_url", "http://127.0.0.1:28768")
        api_key = cfg.get("auth", {}).get("api_key", "")
        _client = _Client(url, api_key=api_key)
    return _client


def _get_user_id() -> str:
    return _load_config().get("user_id", "yang")


def _get_agent_id() -> str:
    return _load_config().get("agent_id", "hermes")


def _get_timeout(operation: str = "add") -> float:
    """从配置读取超时时间（秒）。"""
    cfg = _load_config()
    timeouts = cfg.get("timeout", {})
    return timeouts.get(operation, 300.0)  # 默认300秒


def _get_sender_metadata() -> dict:
    """从 lark-hls-v2 的 _msg_ctx 读取 sender 信息（飞书消息溯源）。"""
    try:
        from hermes_plugins.lark_hls_v2.interceptors import _msg_ctx
        ctx = _msg_ctx.get()
        logger.debug("mem0x: _get_sender_metadata ctx=%s", ctx)
        if not ctx:
            return {}
        return {
            "sender_open_id": ctx.get("user_id", ""),
            "user_name": ctx.get("user_name", ""),
            "chat_id": ctx.get("chat_id", ""),
            "chat_type": ctx.get("chat_type", ""),
            "platform": ctx.get("platform", ""),
            "message_id": ctx.get("message_id", ""),
            "event_message_id": ctx.get("event_message_id", ""),
        }
    except Exception as e:
        logger.debug("mem0x: _get_sender_metadata failed: %s", e)
        return {}


# ═══════════════════════════════════════════════════
# MemoryProvider 接口实现
# ═══════════════════════════════════════════════════

from agent.memory_provider import MemoryProvider


class Mem0RemoteProvider(MemoryProvider):
    """mem0x MemoryProvider（HTTP 远程调用）。"""

    name = "mem0x"

    def __init__(self, config: dict = None):
        self._config = config or {}

    def is_available(self) -> bool:
        """检查服务是否可用。"""
        client = _get_client()
        result = client.try_request("GET", "/health", timeout=2.0)
        return result is not None and result.get("status") in ("ok", "degraded")

    def initialize(self, session_id: str = "", **kwargs) -> None:
        """初始化（无操作，服务端已初始化）。"""
        pass

    def on_session_end(self, messages: list, **kwargs) -> None:
        """会话结束时的回调（空实现）。"""
        pass

    def system_prompt_block(self) -> str:
        """系统提示词注入。"""
        return ""

    def prefetch(self, query: str, session_id: str = "", **kwargs) -> str:
        """预取记忆（注入 system prompt）。包含向量检索 + Neo4j 图谱联想。"""
        client = _get_client()
        # 增大 limit 以容纳 Neo4j 联想结果
        body = {"query": query, "limit": 8, "rerank": True}
        result = client.try_request("POST", "/search", body=body, timeout=_get_timeout("search"))
        if not result:
            return ""

        results = result.get("results", [])
        if not results:
            return ""

        # 分离向量结果和 Neo4j 联想结果
        vector_results = [r for r in results if not r.get("id", "").startswith("neo4j:")]
        neo4j_results = [r for r in results if r.get("id", "").startswith("neo4j:")]

        lines = []
        # 向量结果（取 top5）
        for r in vector_results[:5]:
            mem = r.get("memory", "")
            score = r.get("score", 0)
            if mem:
                lines.append(f"- {mem} (score: {score:.2f})")

        # Neo4j 联想结果（取 top3，作为补充上下文）
        if neo4j_results:
            lines.append("\n[关联实体]")
            for r in neo4j_results[:3]:
                mem = r.get("memory", "")
                if mem:
                    lines.append(f"- {mem}")

        return "\n".join(lines)

    def sync_turn(self, user_msg: str, assistant_msg: str, session_id: str = "", **kwargs) -> None:
        """对话后异步写入记忆。"""
        metadata = _get_sender_metadata()
        logger.debug("mem0x: sync_turn metadata=%s", metadata)

        def _write():
            client = _get_client()
            content = f"User: {user_msg}\nAssistant: {assistant_msg}"
            body = {
                "messages": content,
                "user_id": _get_user_id(),
                "agent_id": _get_agent_id(),
                "infer": True,
            }
            if metadata:
                body["metadata"] = metadata
            client.try_request("POST", "/add", body=body, timeout=_get_timeout("add"))

        threading.Thread(target=_write, daemon=True).start()

    def on_pre_compress(self, messages: list, **kwargs) -> Optional[str]:
        """压缩前抢救。"""
        recent = []
        for msg in messages[-10:]:
            if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
                recent.append(msg)
        if not recent:
            return None

        def _write():
            client = _get_client()
            content = "\n".join(
                f"{m['role']}: {m.get('content', '')}" for m in recent
            )
            client.try_request("POST", "/add", body={
                "messages": content,
                "user_id": _get_user_id(),
                "agent_id": _get_agent_id(),
                "infer": True,
            }, timeout=_get_timeout("add"))

        threading.Thread(target=_write, daemon=True).start()
        return None

    def on_memory_write(self, action: str, target: str, content: str, metadata: dict = None, **kwargs) -> None:
        """MEMORY.md 写入后镜像。"""
        if not content or len(content) < 10:
            return

        def _write():
            client = _get_client()
            client.try_request("POST", "/add", body={
                "messages": content,
                "user_id": _get_user_id(),
                "agent_id": _get_agent_id(),
                "infer": True,
                "metadata": {"source": "MEMORY.md", "action": action},
            }, timeout=_get_timeout("add"))

        threading.Thread(target=_write, daemon=True).start()

    def get_tool_schemas(self) -> List[dict]:
        """返回工具 schema。"""
        return [ADD_SCHEMA, SEARCH_SCHEMA, DELETE_SCHEMA, UPDATE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict) -> str:
        """处理工具调用。返回 JSON 字符串。"""
        client = _get_client()
        
        # PII 脱敏（插件层本地处理）
        if tool_name in ("mem0_add", "mem0_update"):
            content = args.get("content", "")
            if content:
                import re
                # 身份证、手机、邮箱、密码明文 → 脱敏替换
                pii_replacements = [
                    (r'(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)', '[REDACTED_ID]'),
                    (r'(?<!\d)1[3-9]\d{9}(?!\d)', '[REDACTED_PHONE]'),
                    (r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '[REDACTED_EMAIL]'),
                    (r'(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+', r'\1=[REDACTED]'),
                ]
                for pattern, replacement in pii_replacements:
                    content = re.sub(pattern, replacement, content, flags=re.IGNORECASE)
                args["content"] = content
        
        if tool_name == "mem0_add":
            content = args.get("content", "")
            metadata = _get_sender_metadata()
            body = {
                "messages": content,
                "user_id": _get_user_id(),
                "agent_id": _get_agent_id(),
                "infer": False,
            }
            if metadata:
                body["metadata"] = metadata
            result = client.try_request("POST", "/add", body=body, timeout=_get_timeout("add"))

        elif tool_name == "mem0_search":
            query = args.get("query", "")
            top_k = args.get("top_k", 10)
            include_archived = args.get("include_archived", False)
            result = client.try_request("POST", "/search", body={
                "query": query,
                "limit": top_k,
                "rerank": True,
                "include_archived": include_archived,
                "user_id": _get_user_id(),
                "agent_id": _get_agent_id(),
            }, timeout=_get_timeout("search"))

        elif tool_name == "mem0_delete":
            memory_id = args.get("memory_id", "")
            result = client.try_request("POST", "/delete", body={
                "memory_id": memory_id,
            }, timeout=_get_timeout("search"))

        elif tool_name == "mem0_update":
            memory_id = args.get("memory_id", "")
            content = args.get("content", "")
            result = client.try_request("POST", "/update", body={
                "memory_id": memory_id,
                "content": content,
            }, timeout=_get_timeout("search"))

        else:
            result = None

        if result is None:
            return json.dumps({"error": "Request failed or returned None"})
        return json.dumps(result) if isinstance(result, dict) else str(result)

    def shutdown(self) -> None:
        """关闭（无操作）。"""
        pass


# ═══════════════════════════════════════════════════
# 工具 Schema
# ═══════════════════════════════════════════════════

ADD_SCHEMA = {
    "name": "mem0_add",
    "description": "存储持久事实到长期记忆。",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要存储的事实内容"},
        },
        "required": ["content"],
    },
}

SEARCH_SCHEMA = {
    "name": "mem0_search",
    "description": "搜索长期记忆。",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索查询"},
            "top_k": {"type": "integer", "description": "返回结果数量", "default": 10},
            "include_archived": {"type": "boolean", "description": "是否包含归档记忆（被矛盾消解或consolidation归档的旧版本）", "default": False},
        },
        "required": ["query"],
    },
}

DELETE_SCHEMA = {
    "name": "mem0_delete",
    "description": "删除长期记忆。",
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "记忆 ID"},
        },
        "required": ["memory_id"],
    },
}

UPDATE_SCHEMA = {
    "name": "mem0_update",
    "description": "更新长期记忆内容。",
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "记忆 ID"},
            "content": {"type": "string", "description": "新内容"},
        },
        "required": ["memory_id", "content"],
    },
}


# ═══════════════════════════════════════════════════
# 注册入口（Hermes 插件系统调用）
# ═══════════════════════════════════════════════════

_provider: Optional[Mem0RemoteProvider] = None


def register(ctx) -> None:
    """Hermes 插件注册入口。

    ctx 是 _ProviderCollector 实例，调用 ctx.register_memory_provider() 注册。
    激活方式：config.yaml 中设置 memory.provider: mem0x
    """
    global _provider
    _provider = Mem0RemoteProvider()
    ctx.register_memory_provider(_provider)
    logger.info("mem0x plugin registered via ctx.register_memory_provider()")


def get_provider() -> Mem0RemoteProvider:
    """获取 provider 实例。"""
    global _provider
    if _provider is None:
        _provider = Mem0RemoteProvider()
    return _provider
