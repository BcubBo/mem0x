# mem0x 统一 API 网关设计

> 日期：2026-08-25
> 目标：所有请求走统一 `/api` 入口，metadata 在网关层注入，消除各端点遗漏 user_id/agent_id 的问题。
> 同步重构：模块拆分、线程安全、安全架构统一。

---

## 一、现状问题

### 1.1 metadata 遗漏

| 方法 | 路径 | user_id | agent_id | metadata |
|------|------|---------|----------|----------|
| prefetch | /search | ❌ | ❌ | ❌ |
| sync_turn | /add | ✅ | ✅ | ✅ |
| on_pre_compress | /add | ✅ | ✅ | — |
| on_memory_write | /add | ✅ | ✅ | ✅ |
| handle_tool_call mem0_add | /add | ✅ | ✅ | ✅ |
| handle_tool_call mem0_search | /search | ✅ | ✅ | — |
| handle_tool_call mem0_delete | /delete | ❌ | ❌ | — |
| handle_tool_call mem0_update | /update | ❌ | ❌ | — |

### 1.2 代码组织问题

| 文件 | 行数 | 问题 |
|------|------|------|
| `plugin/__init__.py` | 409 | HTTP client + config + provider + 所有业务方法全塞一个文件 |
| `mem0x_server.py` | 1309 | FastAPI app + models + handlers + auth + rate limiting 全混一起 |

### 1.3 线程安全问题

| 位置 | 问题 |
|------|------|
| plugin `_client` 全局变量 | 无锁，多线程竞争 `global _client` |
| plugin `threading.Thread` 异步写入 | 无并发控制，高频对话可能创建大量线程 |
| server `_pending_deletions` | ✅ 已有 `threading.Lock` |
| server Redis 限流 | ✅ asyncio + Redis 原子操作 |

---

## 二、模块拆分

### 2.1 插件端（`plugin/`）

```
plugin/
├── __init__.py          # 仅 register() + get_provider()，≤20 行
├── _client.py           # HTTP 客户端（线程安全，统一 call 出口）
├── _config.py           # 配置加载（单例 + 缓存）
├── provider.py          # Mem0RemoteProvider 类（业务逻辑）
├── plugin.yaml          # 不变
└── mem0x.json.example   # 不变
```

**`__init__.py`（模块入口，≤20 行）：**
```python
from .provider import Mem0RemoteProvider

def register(ctx) -> None:
    ctx.register_provider("mem0x", Mem0RemoteProvider)

def get_provider() -> Mem0RemoteProvider:
    return Mem0RemoteProvider()
```

**`_client.py`（HTTP 客户端）：**
```python
"""线程安全的 HTTP 客户端，统一 /api 出口。"""
import json
import logging
import threading
import urllib.request
from typing import Any, Optional

logger = logging.getLogger("hermes_plugins.mem0x")


class Client:
    """线程安全 HTTP 客户端。线程局部存储避免竞争。"""

    def __init__(self, base_url: str, api_key: str = "",
                 user_id: str = "default", agent_id: str = "hermes"):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self.agent_id = agent_id
        # 每个线程独立的 urllib opener，避免竞争
        self._local = threading.local()

    def _get_opener(self):
        if not hasattr(self._local, "opener"):
            self._local.opener = urllib.request.build_opener()
        return self._local.opener

    def call(self, action: str, timeout: float = 6.0, **payload) -> Optional[Any]:
        """统一出口：自动注入 metadata，发 POST /api。"""
        body = {"action": action, **payload}
        headers = {
            "Content-Type": "application/json",
            "X-User-ID": self.user_id,
            "X-Agent-ID": self.agent_id,
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        req = urllib.request.Request(
            f"{self.base}/api",
            data=json.dumps(body).encode(),
            method="POST",
            headers=headers,
        )
        try:
            opener = self._get_opener()
            with opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            logger.debug("mem0x call(%s) failed: %s", action, e)
            return None
```

**`_config.py`（配置加载）：**
```python
"""配置加载，单例 + 缓存。"""
import json
import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes_plugins.mem0x")

_config: Optional[Dict[str, Any]] = None
_config_lock = threading.Lock()
_config_path: Optional[str] = None


def set_config_path(path: str) -> None:
    global _config_path
    _config_path = path


def load_config() -> Dict[str, Any]:
    global _config
    if _config is not None:
        return _config
    with _config_lock:
        if _config is not None:
            return _config
        # ... 加载逻辑
        return _config


def get_config() -> Dict[str, Any]:
    return load_config()
```

**`provider.py`（业务逻辑）：**
```python
"""Mem0RemoteProvider — MemoryProvider 接口实现。"""
from agent.memory_provider import MemoryProvider
from ._client import Client
from ._config import get_config
# ...

class Mem0RemoteProvider(MemoryProvider):
    name = "mem0x"

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._client: Optional[Client] = None

    def _get_client(self) -> Client:
        if self._client is None:
            cfg = get_config()
            self._client = Client(
                base_url=cfg.get("service_url", "http://127.0.0.1:28768"),
                api_key=cfg.get("auth", {}).get("api_key", ""),
                user_id=cfg.get("user_id", "default"),
                agent_id=cfg.get("agent_id", "hermes"),
            )
        return self._client

    def prefetch(self, query, session_id="", **kwargs):
        result = self._get_client().call("search", timeout=self._get_timeout("search"),
                                         query=query, limit=8, rerank=True)
        # ... 结果处理不变

    def sync_turn(self, user_msg, assistant_msg, session_id="", **kwargs):
        metadata = _get_sender_metadata()
        content = f"User: {user_msg}\nAssistant: {assistant_msg}"
        payload = {"messages": content, "infer": True}
        if metadata:
            payload["metadata"] = metadata
        self._get_client().call("add", timeout=self._get_timeout("add"), **payload)

    # ... 其他方法类似
```

### 2.2 服务端（`mem0x_server.py` → 拆分）

```
mem0x/
├── __init__.py          # app 创建 + lifespan，≤50 行
├── models.py            # 所有 Request/Response 模型
├── auth.py              # verify_api_key + rate limiting
├── unified.py           # POST /api 统一入口
├── handlers.py          # _handle_add / _handle_search / _handle_delete / _handle_update
├── compat.py            # 旧端点（/add /search /delete /update）兼容层
├── audit.py             # 审计日志（SQLite）
└── deletion.py          # 删除确认 token 逻辑
```

**`__init__.py`（app 创建）：**
```python
"""mem0x 服务端入口。"""
from fastapi import FastAPI
from .auth import verify_api_key
from .unified import router as unified_router
from .compat import router as compat_router

app = FastAPI(title="mem0x", lifespan=lifespan)
app.include_router(unified_router)
app.include_router(compat_router)
```

**`models.py`（数据模型）：**
```python
"""所有 Request/Response 模型。"""
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any

class AddRequest(BaseModel):
    messages: str
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    infer: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None

class SearchRequest(BaseModel):
    query: str
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    limit: Optional[int] = None
    rerank: Optional[bool] = None
    include_archived: Optional[bool] = None

class DeleteRequest(BaseModel):
    memory_id: str
    user_id: Optional[str] = None
    confirm_token: Optional[str] = None

class UpdateRequest(BaseModel):
    memory_id: str
    content: str
    user_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class UnifiedRequest(BaseModel):
    action: str  # "add" | "search" | "delete" | "update"
    # 各端点参数（按需使用）
    query: Optional[str] = None
    limit: Optional[int] = None
    rerank: Optional[bool] = None
    include_archived: Optional[bool] = None
    messages: Optional[str] = None
    content: Optional[str] = None
    infer: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None
    memory_id: Optional[str] = None
    confirm_token: Optional[str] = None
```

**`auth.py`（认证 + 限流）：**
```python
"""API Key 验证 + Redis 速率限制。"""
import hashlib
import time
from fastapi import Request, HTTPException
import redis.asyncio as aioredis

_api_key: Optional[str] = None
_redis_client: Optional[aioredis.Redis] = None

def verify_api_key(request: Request):
    """FastAPI 依赖：校验 API Key。"""
    required_key = _get_api_key()
    if not required_key:
        return
    key = request.headers.get("X-API-Key", "")
    if not key:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            key = auth[7:]
    if not key or key != required_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

async def check_rate_limit_async(path: str, api_key: str = "anonymous") -> None:
    """异步速率限制（Redis 优先，降级为内存）。"""
    # ... 实现不变
```

**`unified.py`（统一入口）：**
```python
"""POST /api 统一入口。"""
from fastapi import APIRouter, Request, Depends, HTTPException
from .models import UnifiedRequest
from .auth import verify_api_key, check_rate_limit_async
from .handlers import handle_add, handle_search, handle_delete, handle_update

router = APIRouter()

def _extract_identity(request: Request) -> tuple[str, str]:
    """从 headers 提取 user_id / agent_id（全端点统一）。"""
    user_id = request.headers.get("X-User-ID") or "default"
    agent_id = request.headers.get("X-Agent-ID") or "hermes"
    return user_id, agent_id

@router.post("/api", dependencies=[Depends(verify_api_key)])
async def unified_api(req: UnifiedRequest, request: Request):
    user_id, agent_id = _extract_identity(request)
    await check_rate_limit_async("/api", request.headers.get("X-API-Key", "anonymous"))

    handlers = {
        "add": handle_add,
        "search": handle_search,
        "delete": handle_delete,
        "update": handle_update,
    }
    handler = handlers.get(req.action)
    if not handler:
        raise HTTPException(400, detail=f"Unknown action: {req.action}")
    return await handler(req, user_id, agent_id, request)
```

**`handlers.py`（业务逻辑）：**
```python
"""各 action 的处理函数。从现有端点提取。"""
async def handle_add(req, user_id, agent_id, request):
    """添加记忆。"""
    # ... 现有 /add 端点逻辑，user_id/agent_id 从参数获取

async def handle_search(req, user_id, agent_id, request):
    """搜索记忆。"""
    # ... 现有 /search 端点逻辑

async def handle_delete(req, user_id, agent_id, request):
    """删除记忆。"""
    # ... 现有 /delete 端点逻辑

async def handle_update(req, user_id, agent_id, request):
    """更新记忆。"""
    # ... 现有 /update 端点逻辑
```

**`compat.py`（兼容层）：**
```python
"""旧端点兼容（deprecated）。"""
import logging
from fastapi import APIRouter, Request, Depends
from .models import AddRequest, SearchRequest, DeleteRequest, UpdateRequest
from .auth import verify_api_key
from .unified import _extract_identity
from .handlers import handle_add, handle_search, handle_delete, handle_update

router = APIRouter()
logger = logging.getLogger("mem0x.compat")

@router.post("/add", dependencies=[Depends(verify_api_key)])
async def add_memory(req: AddRequest, request: Request):
    logger.warning("DEPRECATED: POST /add → use POST /api {action: 'add'}")
    user_id, agent_id = _extract_identity(request)
    # 兼容：header 没有 user_id 时从 body 取
    user_id = user_id if user_id != "default" else (req.user_id or "default")
    from .models import UnifiedRequest
    unified = UnifiedRequest(action="add", messages=req.messages,
                             infer=req.infer, metadata=req.metadata)
    return await handle_add(unified, user_id, agent_id, request)

# ... /search, /delete, /update 类似
```

---

## 三、安全架构统一

### 3.1 认证链路

```
插件 Client.call()
  ├─ header: X-API-Key (from plugin config)
  ├─ header: X-User-ID (from plugin config)
  └─ header: X-Agent-ID (from plugin config)
      │
      ▼
服务端 verify_api_key (FastAPI Depends)
  ├─ 检查 X-API-Key 或 Authorization: Bearer
  ├─ 未配置 key 时免认证（兼容升级）
  └─ 失败 → 401
      │
      ▼
extract_identity (unified.py)
  ├─ X-User-ID header → "default"
  └─ X-Agent-ID header → "hermes"
      │
      ▼
check_rate_limit_async (Redis)
  ├─ 滑动窗口限流
  └─ Redis 不可用时降级为内存模式
```

### 3.2 安全模块复用

现有 `security/` 模块不改，handlers 直接调用：

```python
# handlers.py
from security.pipeline import redact_pii, redact_names
from security.injection_guard import validate_memory_content
from security.dedup import is_duplicate
from security.conflict_resolver import resolve_conflicts

async def handle_add(req, user_id, agent_id, request):
    # 1. 注入防御
    is_valid, cleaned, reason = validate_memory_content(req.messages)
    if not is_valid:
        raise HTTPException(400, detail=f"Rejected: {reason}")

    # 2. PII 脱敏
    cleaned = redact_pii(cleaned)

    # 3. 去重检查
    # 4. 写入记忆
    # 5. 冲突消解
```

### 3.3 并发安全

| 组件 | 策略 |
|------|------|
| 插件 `_client` | `threading.local()` 存储 opener，每线程独立，无竞争 |
| 插件 `sync_turn` 等 | `threading.Thread(daemon=True)` 异步写入，不阻塞主流程 |
| 插件 `_config` | `threading.Lock` 保护首次加载，后续读无锁（immutable） |
| 服务端 Redis 限流 | asyncio + Redis Pipeline 原子操作 |
| 服务端 `_pending_deletions` | `threading.Lock` 保护字典读写 |
| 服务端审计日志 | SQLite 写入（单文件，WAL 模式） |

---

## 四、数据模型

### 4.1 插件端请求格式

```json
{
  "action": "search",
  "query": "用户偏好",
  "limit": 8,
  "rerank": true
}
```

### 4.2 Headers（插件自动注入）

| Header | 来源 | 必填 |
|--------|------|------|
| X-User-ID | plugin config `user_id` | 否（有 fallback） |
| X-Agent-ID | plugin config `agent_id` | 否（有 fallback） |
| X-API-Key | plugin config `auth.api_key` | 否 |
| Content-Type | 固定 `application/json` | 是 |

### 4.3 用户 ID 解析优先级

服务端统一处理，所有端点一致：

```
X-User-ID header → body.user_id → env MEM0X_DEFAULT_USER → "default"
```

### 4.4 响应格式

```json
// 成功
{"status": "ok", "data": {...}}

// 失败
{"status": "error", "detail": "..."}
```

---

## 五、文件变更清单

### 插件端（`plugin/`）

| 文件 | 操作 | 说明 |
|------|------|------|
| `__init__.py` | **重写** | 409 行 → ≤20 行，仅 register + get_provider |
| `_client.py` | **新建** | 线程安全 HTTP 客户端，统一 /api 出口 |
| `_config.py` | **新建** | 配置加载，单例 + 缓存 + Lock |
| `provider.py` | **新建** | Mem0RemoteProvider 业务逻辑 |

### 服务端（`mem0x/`）

| 文件 | 操作 | 说明 |
|------|------|------|
| `__init__.py` | **新建** | app 创建 + lifespan |
| `models.py` | **新建** | 所有 Request/Response 模型 |
| `auth.py` | **新建** | verify_api_key + rate limiting |
| `unified.py` | **新建** | POST /api 统一入口 |
| `handlers.py` | **新建** | 业务逻辑 handler |
| `compat.py` | **新建** | 旧端点兼容层（deprecated 日志） |
| `audit.py` | **新建** | 审计日志 |
| `deletion.py` | **新建** | 删除确认 token 逻辑 |
| `mem0x_server.py` | **删除** | 内容全部拆分到上述文件 |

---

## 六、兼容策略

| 阶段 | 说明 |
|------|------|
| Phase 1 | 新增 `/api` 端点 + 模块拆分，插件切换到 `/api`，旧端点保留 |
| Phase 2 | 旧端点标记 deprecated（日志警告），30 天后移除 |

---

## 七、测试计划

1. **线程安全测试**：并发调用 `_client.call()` 无竞争
2. **认证测试**：无 key 免认证、错误 key 401、正确 key 通过
3. **限流测试**：Redis 正常 + Redis 不可用降级
4. **集成测试**：`/api` 四个 action 端到端
5. **兼容测试**：旧端点仍可用 + deprecated 日志
6. **回归测试**：prefetch user_id 不再是 "default"
