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
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("hermes_plugins.mem0x")

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mem0x")


def _retry(fn: Callable, max_retries: int = 3, base_delay: float = 1.0,
           validate: Callable = None) -> Any:
    """带指数退避的重试包装。失败后 1s, 2s, 4s 重试。

    validate: 可选校验函数，返回 False 视为失败触发重试。
    用于 client.call() 等吞掉异常返回 None 的场景。
    """
    for attempt in range(max_retries):
        try:
            result = fn()
            if validate and not validate(result):
                raise RuntimeError(f"validate failed: {result}")
            return result
        except Exception as e:
            if attempt == max_retries - 1:
                logger.error("mem0x: 操作失败（%d次重试已耗尽）: %s", max_retries, e)
                return None
            delay = base_delay * (2 ** attempt)
            logger.warning("mem0x: 第%d次重试（%.1fs后）: %s", attempt + 1, delay, e)
            time.sleep(delay)


# ═══════════════════════════════════════════════════
# HTTP 客户端（零依赖）
# ═══════════════════════════════════════════════════

class _Client:
    """轻量 HTTP 客户端（urllib，零依赖）。"""

    def __init__(self, base_url: str, api_key: str = ""):
        self.base = base_url.rstrip("/")
        self.api_key = api_key

    def _build_headers(self, extra: dict = None) -> dict:
        """构建请求 header，自动注入身份和上下文信息。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        # 身份
        headers["X-User-ID"] = _get_user_id()
        headers["X-Agent-ID"] = _get_agent_id()
        # 上下文（来自飞书消息或 kwargs）
        ctx = _get_context()
        _HEADER_MAP = {
            "session_id": "X-Session-ID",
            "platform":   "X-Platform",
            "chat_id":    "X-Chat-ID",
            "chat_type":  "X-Chat-Type",
            "request_id": "X-Request-ID",
        }
        for key, header in _HEADER_MAP.items():
            val = ctx.get(key, "")
            if val:
                headers[header] = val
        headers["X-Source"] = "plugin"
        if extra:
            headers.update(extra)
        return headers

    def request(self, method: str, path: str, body: Any = None,
                timeout: float = 6.0, headers: dict = None) -> Any:
        data = json.dumps(body).encode() if body else None
        if headers is None:
            headers = self._build_headers()
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def call(self, action: str, params: dict = None,
             timeout: float = None) -> Optional[Any]:
        """统一 API 调用。通过 POST /api 发送，自动注入所有 header。"""
        if timeout is None:
            timeout = _get_timeout(action)
        body = {"action": action, "params": params or {}}
        return self.try_request("POST", "/api", body=body, timeout=timeout)

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
    return _load_config().get("user_id", "default")


def _get_agent_id() -> str:
    return _load_config().get("agent_id", "hermes")


_DEFAULT_TIMEOUTS = {
    "add": 30.0,
    "search": 10.0,
    "delete": 10.0,
    "update": 30.0,
}


def _get_timeout(operation: str = "add") -> float:
    """从配置读取超时时间（秒），未配置时按 operation 给合理默认值。"""
    cfg = _load_config()
    timeouts = cfg.get("timeout", {})
    default = _DEFAULT_TIMEOUTS.get(operation, 30.0)
    return timeouts.get(operation, default)


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


def _get_context() -> dict:
    """构建请求上下文（用于 header 注入）。

    优先从飞书 _msg_ctx 获取，fallback 到空字符串。
    """
    try:
        from hermes_plugins.lark_hls_v2.interceptors import _msg_ctx
        ctx = _msg_ctx.get()
        if ctx:
            return {
                "session_id": ctx.get("session_id", ""),
                "platform":   ctx.get("platform", ""),
                "chat_id":    ctx.get("chat_id", ""),
                "chat_type":  ctx.get("chat_type", ""),
                "request_id": ctx.get("message_id", ""),
            }
    except Exception:
        pass
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
        result = client.call("search", {"query": query, "limit": 8, "rerank": True})
        if not result:
            return ""

        results = result.get("results", [])
        if not results:
            return ""

        lines = []
        # 取 top5 结果
        for r in results[:5]:
            mem = r.get("memory", "")
            score = r.get("score", 0)
            if mem:
                lines.append(f"- [MEMORY-DATA] {mem} [/MEMORY-DATA] (score: {score:.2f})")

        return "\n".join(lines)

    def sync_turn(self, user_msg: str, assistant_msg: str, session_id: str = "", **kwargs) -> None:
        """对话后异步写入记忆。"""
        metadata = _get_sender_metadata()
        logger.debug("mem0x: sync_turn metadata=%s", metadata)

        def _write():
            client = _get_client()
            content = f"User: {user_msg}\nAssistant: {assistant_msg}"
            params = {"messages": content, "infer": True}
            if metadata:
                params["metadata"] = metadata
            _retry(lambda: client.call("add", params), validate=lambda r: r is not None)

        _pool.submit(_write)

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
            _retry(lambda: client.call("add", {"messages": content, "infer": True}), validate=lambda r: r is not None)

        _pool.submit(_write)
        return None

    def on_memory_write(self, action: str, target: str, content: str, metadata: dict = None, **kwargs) -> None:
        """MEMORY.md 写入后镜像。"""
        if not content or len(content) < 10:
            return

        def _write():
            client = _get_client()
            _retry(lambda: client.call("add", {
                "messages": content,
                "infer": True,
                "metadata": {"source": "MEMORY.md", "action": action},
            }), validate=lambda r: r is not None)

        _pool.submit(_write)

    def get_tool_schemas(self) -> List[dict]:
        """返回工具 schema。"""
        return [ADD_SCHEMA, SEARCH_SCHEMA, DELETE_SCHEMA, UPDATE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict) -> str:
        """处理工具调用。返回 JSON 字符串。"""
        from ._pii import redact_pii
        client = _get_client()

        # PII 脱敏（插件层本地处理）
        if tool_name in ("mem0_add", "mem0_update"):
            content = args.get("content", "")
            if content:
                args["content"] = redact_pii(content)
        
        if tool_name == "mem0_add":
            content = args.get("content", "")
            metadata = _get_sender_metadata()
            params = {"messages": content, "infer": False}
            if metadata:
                params["metadata"] = metadata
            result = client.call("add", params)

        elif tool_name == "mem0_search":
            result = client.call("search", {
                "query": args.get("query", ""),
                "limit": args.get("top_k", 10),
                "rerank": True,
                "include_archived": args.get("include_archived", False),
            })

        elif tool_name == "mem0_delete":
            result = client.call("delete", {
                "memory_id": args.get("memory_id", ""),
            })

        elif tool_name == "mem0_update":
            result = client.call("update", {
                "memory_id": args.get("memory_id", ""),
                "content": args.get("content", ""),
            })

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

    register_memory_provider 会读取 provider.get_tool_schemas() 返回的所有工具
    并通过 inject_memory_provider_tools 注入到 agent，包括 search/add/update/delete。
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
