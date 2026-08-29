"""api_server — mem0x 独立服务入口

FastAPI HTTP 服务，提供 /add, /search, /delete, /health 端点。
"""
from __future__ import annotations

import logging
import os
import sys
import asyncio
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── 日志配置 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
# 抑制 Qdrant scroll 日志（高频刷屏），保留 embeddings 等关键日志
class _QdrantScrollFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return "qdrant:6333" not in msg or "scroll" not in msg

logging.getLogger("httpx").addFilter(_QdrantScrollFilter())
logging.getLogger("httpx2").addFilter(_QdrantScrollFilter())
logger = logging.getLogger("mem0x")

# ── 确保项目根目录在 sys.path ──
PROJECT_ROOT = str(Path(__file__).resolve().parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi import FastAPI, HTTPException, Request, Depends
from pydantic import BaseModel, Field

# ── 延迟导入（配置加载后才初始化） ──
from wrapper.mem0_runtime import get_memory, load_config
from wrapper.salience import boost_salience_for_results, register as salience_register, delete as salience_delete
from wrapper import working_memory
from wrapper import auto_expire
from wrapper import consolidation
from wrapper import core_memory
from wrapper import evolve_mem
from wrapper import reflect
from wrapper import hot_archive
from wrapper import reconcile
from wrapper import version_tracker
from security.pipeline import safe_add
from security.scoring import score_and_rank
from security.degradation import DegradationTracker


# ═══════════════════════════════════════════════════
# Request / Response Models
# ═══════════════════════════════════════════════════

class AddRequest(BaseModel):
    messages: Any = Field(..., description="消息内容（str 或 list[dict]）")
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    expiration_date: Optional[str] = None
    infer: bool = Field(default=False, description="是否用 LLM 提取事实")


class SearchRequest(BaseModel):
    query: str = Field(..., description="搜索查询")
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=100)
    rerank: bool = Field(default=True, description="是否 rerank")
    before: Optional[str] = Field(default=None, description="时间上限 ISO 格式")
    after: Optional[str] = Field(default=None, description="时间下限 ISO 格式")
    include_archived: bool = Field(default=False, description="是否包含归档记忆")
    tags: Optional[List[str]] = Field(default=None, description="按 tags 过滤（metadata.tags）")
    tags_match: str = Field(default="any", description="tags 匹配模式: any=任一匹配, all=全部匹配")


class DeleteRequest(BaseModel):
    memory_id: str = Field(..., description="记忆 ID")
    confirm_token: Optional[str] = Field(default=None, description="确认 token（/delete/confirm 时必填）")


class UpdateRequest(BaseModel):
    memory_id: str = Field(..., description="记忆 ID")
    content: str = Field(..., description="新内容")
    metadata: Optional[Dict[str, Any]] = None


class UnifiedRequest(BaseModel):
    """统一 API 入口请求。action 指定操作，params 传递该操作的参数。"""
    action: str = Field(..., description="操作类型: add/search/delete/update")
    params: Dict[str, Any] = Field(default_factory=dict, description="操作参数")


def _extract_identity(request: Request) -> dict:
    """从 header 提取请求身份，返回统一上下文。

    优先级：header > body > 默认值
    所有字段均可为空字符串（不影响业务逻辑）。
    """
    return {
        "user_id":    request.headers.get("X-User-ID", ""),
        "agent_id":   request.headers.get("X-Agent-ID", ""),
        "session_id": request.headers.get("X-Session-ID", ""),
        "platform":   request.headers.get("X-Platform", ""),
        "chat_id":    request.headers.get("X-Chat-ID", ""),
        "chat_type":  request.headers.get("X-Chat-Type", ""),
        "request_id": request.headers.get("X-Request-ID", ""),
        "source":     request.headers.get("X-Source", ""),
    }


# ═══════════════════════════════════════════════════
# Lifespan
# ═══════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化 mem0 + auto_expire，关闭时清理。"""
    logger.info("mem0x 启动中...")
    config = load_config()

    # 加载脱敏名称映射（从 config.json 的 redact_names 字段）
    try:
        from security.pipeline import load_redact_names as _load_pipeline_redact
        _load_pipeline_redact(config)
    except Exception as e:
        logger.warning("pipeline redact_names 加载失败: %s", e)

    # 初始化 mem0 单例
    try:
        mem = get_memory(config)
        logger.info("mem0 初始化成功")
    except Exception as e:
        logger.error("mem0 初始化失败: %s", e)


    # 启动 auto_expire 后台线程
    try:
        auto_expire.start(get_memory)
        logger.info("auto_expire 已启动")
    except Exception as e:
        logger.warning("auto_expire 启动失败: %s", e)

    # 启动 consolidation 后台线程
    try:
        consolidation.start(get_memory)
        logger.info("consolidation 已启动")
    except Exception as e:
        logger.warning("consolidation 启动失败: %s", e)

    # 启动 evolve_mem 后台线程
    try:
        evolve_mem.start(get_memory)
        logger.info("evolve_mem 已启动")
    except Exception as e:
        logger.warning("evolve_mem 启动失败: %s", e)

    # 启动 reflect 后台线程
    try:
        reflect.start(get_memory)
        logger.info("reflect 已启动")
    except Exception as e:
        logger.warning("reflect 启动失败: %s", e)

    # 启动 hot_archive 后台线程
    try:
        hot_archive.start(get_memory)
        logger.info("hot_archive 已启动")
    except Exception as e:
        logger.warning("hot_archive 启动失败: %s", e)

    # 启动 reconcile 对账后台线程
    try:
        reconcile.start_reconcile_thread()
        logger.info("reconcile 已启动")
    except Exception as e:
        logger.warning("reconcile 启动失败: %s", e)

    # 启动补偿队列 worker
    try:
        from security.compensation import start as comp_start
        from security.pipeline import safe_add as _safe_add_fn
        from wrapper.index_sync import compensate_delete as _comp_delete_fn
        from wrapper.index_sync import compensate_merge as _comp_merge_fn
        comp_start(handlers={"add": _safe_add_fn, "delete": _comp_delete_fn, "merge": _comp_merge_fn})
        logger.info("补偿队列已启动")
    except Exception as e:
        logger.warning("补偿队列启动失败: %s", e)

    yield

    # 关闭
    from security.compensation import stop as comp_stop
    comp_stop()
    auto_expire.stop()
    consolidation.stop()
    evolve_mem.stop()
    reflect.stop()
    hot_archive.stop()
    reconcile.stop()
    from wrapper.fts5_store import close_fts5
    close_fts5()
    logger.info("mem0x 已关闭")


app = FastAPI(
    title="mem0x",
    description="自托管 AI 记忆增强服务",
    version="0.2.0",
    lifespan=lifespan,
)


# ═══════════════════════════════════════════════════
# 并发控制：端点信号量
# ═══════════════════════════════════════════════════
_endpoint_semaphore = asyncio.Semaphore(50)


# ═══════════════════════════════════════════════════
# Helper functions
# ═══════════════════════════════════════════════════

async def _update_usage_stats_sync(memory_instance, memory_ids: list):
    """批量更新被搜索记忆的使用维度字段（并发执行，减少 N+1）。"""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    valid_ids = [mid for mid in memory_ids if mid]
    if not valid_ids:
        return

    _qdrant_sem = asyncio.Semaphore(3)

    async def _get_meta(mid):
        async with _qdrant_sem:
            try:
                existing = await memory_instance.get(mid)
                return mid, existing.get("metadata", {}) if existing else {}
            except Exception as e:
                logger.warning("get_memory metadata failed for %s: %s", mid, e)
                return mid, {}

    results = await asyncio.gather(*[_get_meta(mid) for mid in valid_ids])

    # 批量更新
    update_tasks = []
    for mid, meta in results:
        current_count = (meta or {}).get("search_count", 0)
        new_count = int(current_count) + 1 if isinstance(current_count, (int, float)) else 1
        update_tasks.append(
            memory_instance.update(mid, metadata={"search_count": new_count, "last_accessed_at": now})
        )
    await asyncio.gather(*update_tasks, return_exceptions=True)

import hashlib
import hmac
import secrets
import sqlite3

_DELETE_CONFIRM_TTL = 300  # 5分钟有效期
# ⚠️ 多 worker/gunicorn 场景必须显式设置 MEM0X_DELETE_SECRET 环境变量
# 否则每个 worker 各自生成不同 secret，token 跨 worker 不互通
_delete_secret_raw = os.environ.get("MEM0X_DELETE_SECRET")
if not _delete_secret_raw:
    # 尝试从文件加载（持久化，重启不丢失）
    _secret_file = os.path.join(
        os.environ.get("MEM0X_DATA_DIR", "data"), ".delete_secret"
    )
    try:
        if os.path.exists(_secret_file):
            with open(_secret_file, "r") as f:
                _delete_secret_raw = f.read().strip()
        else:
            _delete_secret_raw = secrets.token_hex(16)
            os.makedirs(os.path.dirname(_secret_file), exist_ok=True)
            with open(_secret_file, "w") as f:
                f.write(_delete_secret_raw)
            logger.info("MEM0X_DELETE_SECRET 已生成并持久化到 %s", _secret_file)
    except Exception as e:
        logger.warning("DELETE_SECRET 持久化失败，回退随机生成: %s", e)
        _delete_secret_raw = secrets.token_hex(16)
_DELETE_SECRET: str = _delete_secret_raw
_pending_deletions: dict[str, dict] = {}  # {token: {memory_id, expires_at, user_id, used}}
_pending_deletions_lock = threading.Lock()

# ── 审计日志 SQLite ──
_audit_db_path = None


def _get_audit_db() -> sqlite3.Connection:
    """获取审计日志数据库连接。"""
    global _audit_db_path
    if _audit_db_path is None:
        _audit_db_path = os.path.join(
            os.environ.get("MEM0X_DATA_DIR", "data"),
            "delete_audit.db",
        )
    conn = sqlite3.connect(_audit_db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delete_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            user_id TEXT,
            api_key_hash TEXT,
            created_at REAL NOT NULL,
            confirmed_at REAL,
            cancelled_at REAL,
            action TEXT NOT NULL DEFAULT 'pending'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token ON delete_audit(token)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_id ON delete_audit(memory_id)")
    conn.commit()
    return conn


def _log_delete_event(token: str, memory_id: str, user_id: str = None,
                      api_key: str = None, action: str = "pending") -> None:
    """记录删除审计事件。"""
    try:
        conn = _get_audit_db()
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16] if api_key else None
        if action == "pending":
            conn.execute(
                "INSERT INTO delete_audit (token, memory_id, user_id, api_key_hash, created_at, action) VALUES (?, ?, ?, ?, ?, ?)",
                (token, memory_id, user_id, api_key_hash, time.time(), action),
            )
        elif action == "confirmed":
            conn.execute(
                "UPDATE delete_audit SET confirmed_at=?, action='confirmed' WHERE token=? AND action='pending'",
                (time.time(), token),
            )
        elif action == "cancelled":
            conn.execute(
                "UPDATE delete_audit SET cancelled_at=?, action='cancelled' WHERE token=? AND action='pending'",
                (time.time(), token),
            )
        conn.commit()
    except Exception as e:
        logger.warning("审计日志写入失败: %s", e)


def _generate_delete_token(memory_id: str, user_id: str = None, api_key: str = None) -> str:
    """生成删除确认 token（HMAC-based，5分钟有效，绑定 user_id）。"""
    secret = _DELETE_SECRET
    timestamp = str(int(time.time()))
    token = hmac.new(secret.encode(), f"{memory_id}:{timestamp}".encode(), hashlib.sha256).hexdigest()[:16]
    with _pending_deletions_lock:
        _pending_deletions[token] = {
            "memory_id": memory_id,
            "expires_at": time.time() + _DELETE_CONFIRM_TTL,
            "timestamp": timestamp,
            "user_id": user_id,
            "api_key_hash": hashlib.sha256(api_key.encode()).hexdigest()[:16] if api_key else None,
            "used": False,
        }
    _log_delete_event(token, memory_id, user_id, api_key, "pending")
    return token


def _verify_delete_token(token: str, api_key: str = None) -> Optional[str]:
    """验证删除确认 token（一次性 + user_id 绑定）。返回 memory_id 或 None。"""
    with _pending_deletions_lock:
        entry = _pending_deletions.get(token)
        if not entry:
            return None
        if entry["used"]:
            logger.warning("token 重放拒绝: %s", token[:8])
            return None
        if time.time() > entry["expires_at"]:
            del _pending_deletions[token]
            return None
        # 验证 api_key 绑定
        if api_key:
            api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
            if entry["api_key_hash"] and entry["api_key_hash"] != api_key_hash:
                logger.warning("token api_key 不匹配: %s", token[:8])
                return None
        entry["used"] = True  # 标记已使用
    _log_delete_event(token, entry["memory_id"], action="confirmed")
    return entry["memory_id"]


def _cancel_delete_token(token: str) -> bool:
    """撤销删除 token。返回是否成功。"""
    with _pending_deletions_lock:
        entry = _pending_deletions.pop(token, None)
    if not entry:
        return False
    _log_delete_event(token, entry["memory_id"], action="cancelled")
    return True

# ═══════════════════════════════════════════════════
# API Key 认证
# ═══════════════════════════════════════════════════

import hashlib
import secrets

_api_key_cache: Optional[str] = None
_api_key_cache_at: float = 0


def _get_api_key() -> Optional[str]:
    """从 config.json 读取 api_key（缓存5分钟）。"""
    global _api_key_cache, _api_key_cache_at
    if _api_key_cache is not None and time.time() - _api_key_cache_at < 300:
        return _api_key_cache
    try:
        cfg = load_config()
        key = cfg.get("server", {}).get("api_key", "")
        if key:
            _api_key_cache = key
            _api_key_cache_at = time.time()
            return key
    except Exception:
        logger.debug("api_key load failed", exc_info=True)
    return None


def verify_api_key(request: Request):
    """FastAPI 依赖：API Key 验证。"""
    required_key = _get_api_key()
    if not required_key:
        logger.warning("⚠️ API key未配置，/stats等端点免认证运行（仅限开发环境）")
        return  # 未配置 api_key，免认证（向后兼容）

    # 从 X-API-Key 或 Authorization: Bearer <key> 取
    key = request.headers.get("X-API-Key", "")
    if not key:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            key = auth[7:]

    if not key or key != required_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ═══════════════════════════════════════════════════
# Redis 速率限制
# ═══════════════════════════════════════════════════

import redis.asyncio as aioredis

_redis_client: Optional[aioredis.Redis] = None


def _get_redis() -> Optional[aioredis.Redis]:
    """获取 Redis 连接（懒加载）。"""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        cfg = load_config()
        redis_cfg = cfg.get("redis", {})
        host = redis_cfg.get("host", "redis")
        port = redis_cfg.get("port", 6379)
        db = redis_cfg.get("db", 0)
        _redis_client = aioredis.Redis(
            host=host, port=port, db=db,
            decode_responses=True,
            max_connections=20,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
        logger.info("Redis 连接成功: %s:%d", host, port)
        return _redis_client
    except Exception as e:
        logger.warning("Redis 连接失败: %s，限流降级为内存模式", e)
        return None


# 端点限流配置：{端点: (最大请求数, 时间窗口秒)}
_RATE_LIMITS = {
    "/add": (30, 60),        # LLM调用贵
    "/search": (60, 60),     # 轻量
    "/delete": (10, 60),     # 低频
    "/delete/confirm": (10, 60),
    "/update": (20, 60),     # 中频
    "/expire": (5, 60),      # 后台任务
    "/consolidate": (5, 60),
    "/evolve": (5, 60),
    "/reflect": (5, 60),
    "/reconcile": (3, 60),        # 对账：低频
    "/reconcile/stats": (30, 60), # 统计查询：轻量
}
_DEFAULT_LIMIT = (120, 60)  # 其他端点


async def _check_rate_limit(key: str, max_requests: int, window: int) -> bool:
    """滑动窗口限流。返回 True 表示允许，False 表示超限。"""
    r = _get_redis()
    if r is None:
        return True  # Redis 不可用时放行

    now = time.time()
    window_start = now - window

    pipe = r.pipeline()
    # 移除窗口外的旧请求
    pipe.zremrangebyscore(key, 0, window_start)
    # 添加当前请求
    pipe.zadd(key, {str(now): now})
    # 设置过期时间
    pipe.expire(key, window)
    # 统计窗口内请求数
    pipe.zcard(key)
    results = await pipe.execute()

    count = results[-1]
    return count <= max_requests


async def rate_limit(request: Request):
    """FastAPI 异步依赖：速率限制。超限时抛 429。"""
    path = request.url.path
    if path in ("/health", "/reflect/health"):
        return
    api_key = request.headers.get("X-API-Key", "anonymous")
    await check_rate_limit_async(path, api_key)


async def check_rate_limit_async(path: str, api_key: str = "anonymous") -> None:
    """异步限流检查，超限时抛异常。"""
    max_requests, window = _RATE_LIMITS.get(path, _DEFAULT_LIMIT)
    key = f"ratelimit:{path}:{api_key}"
    allowed = await _check_rate_limit(key, max_requests, window)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {max_requests} requests per {window}s"
        )


# ═══════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════

@app.get("/health")
async def health(request: Request):
    """健康检查。未认证时只返回 status，不暴露内部状态。"""
    from wrapper.mem0_runtime import _memory_instance
    mem_ok = _memory_instance is not None
    # 未认证时只返回基础状态
    api_key = request.headers.get("X-API-Key")
    if not api_key or not _get_api_key():
        return {"status": "ok" if mem_ok else "degraded"}
    degraded = DegradationTracker.get_degraded_components()
    return {
        "status": "ok" if mem_ok else "degraded",
        "mem0": mem_ok,
        "degraded_components": degraded,
    }


@app.post("/add", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def add_memory(req: AddRequest, request: Request):
    """安全写入记忆。

    链路：注入防御 → PII脱敏 → 去重 → 矛盾消解 → 语义判重 → 写入
    user_id 优先级：请求头 X-User-ID > 请求体 user_id > 默认 "bo"
    """
    async with _endpoint_semaphore:
        return await _add_memory_impl(req, request)


async def _add_memory_impl(req: AddRequest, request: Request):
    memory = get_memory()
    start = time.time()

    # 从请求头或请求体获取 user_id/agent_id
    user_id = request.headers.get("X-User-ID") or req.user_id or os.environ.get("MEM0X_DEFAULT_USER", "default")
    agent_id = request.headers.get("X-Agent-ID") or req.agent_id or "hermes"

    # 构建 filters（mem0 2.0+ 必须有 user_id/agent_id/run_id 之一）
    filters = {"user_id": user_id, "agent_id": agent_id}

    # 提取文本
    if isinstance(req.messages, str):
        content = req.messages
    elif isinstance(req.messages, list):
        content = " ".join(
            m.get("content", "") for m in req.messages if isinstance(m, dict)
        )
    else:
        content = str(req.messages)

    logger.debug("📥 add: user=%s, agent=%s, content_len=%d", user_id, agent_id, len(content))

    # 训练 BM25 IDF（同步，放线程池）
    try:
        from wrapper.sparse_vector import get_bm25_encoder
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, get_bm25_encoder().encode, content)
    except Exception as e:
        logger.debug("BM25 IDF 训练失败: %s", e)

    # 初始化使用维度字段
    usage_metadata = {
        "search_count": 0,
        "last_accessed_at": None,
        "update_count": 0,
        "reference_count": 0,
    }
    # 合并用户提供的 metadata
    if req.metadata:
        usage_metadata.update(req.metadata)

    # 安全写入链路
    result = await safe_add(
        memory, content, filters,
        user_id=user_id, agent_id=agent_id,
        metadata=usage_metadata, expiration_date=req.expiration_date,
        infer=req.infer,
    )

    # 写入后：注册 salience + 版本追踪（异步化 SQLite I/O）
    memory_id = result.get("memory_id")
    if memory_id and result.get("action") in ("added", "conflict"):
        loop = asyncio.get_running_loop()

        try:
            await loop.run_in_executor(None, salience_register, memory_id, content[:200])
        except Exception as e:
            logger.warning("salience register 失败: %s", e)

        # 工作记忆：写入高频上下文
        try:
            await loop.run_in_executor(None, working_memory.add, memory_id, content, user_id)
        except Exception as e:
            logger.warning("working_memory add 失败: %s", e)

        # 版本追踪：保存初始版本
        try:
            await loop.run_in_executor(None, version_tracker.save_version, memory_id, content, None, "create")
        except Exception as e:
            logger.warning("version_tracker init 失败: %s", e)

        # tags hook：提取实体写入 Qdrant payload（Qdrant set_payload，同步调用需放线程池）
        try:
            from wrapper.tags_hook import on_add
            await loop.run_in_executor(None, on_add, memory, memory_id, content)
        except Exception as e:
            logger.debug("tags_hook on_add 失败: %s", e)

    elapsed_ms = int((time.time() - start) * 1000)
    result["elapsed_ms"] = elapsed_ms
    return result


@app.post("/search", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def search_memory(req: SearchRequest, request: Request):
    """搜索记忆。

    链路：向量检索 → 5维打分 → rerank → salience boost
    user_id 优先级：请求头 X-User-ID > 请求体 user_id > 默认 "bo"
    """
    async with _endpoint_semaphore:
        return await _search_memory_impl(req, request)


async def _search_memory_impl(req: SearchRequest, request: Request):
    memory = get_memory()
    start = time.time()

    # 从请求头或请求体获取 user_id/agent_id
    user_id = request.headers.get("X-User-ID") or req.user_id or os.environ.get("MEM0X_DEFAULT_USER", "default")
    agent_id = request.headers.get("X-Agent-ID") or req.agent_id or "hermes"
    logger.info("🔍 search: user_id=%s, agent_id=%s, req.user_id=%s, req.agent_id=%s", user_id, agent_id, req.user_id, req.agent_id)
    # 打印请求体原文（调试用）
    import json as _json
    logger.debug("🔍 search: body=%s", _json.dumps({"query": "***", "user_id": req.user_id, "agent_id": req.agent_id}, ensure_ascii=False))

    # 构建 filters（mem0 2.0+ 必须有 user_id/agent_id/run_id 之一）
    filters = {"user_id": user_id, "agent_id": agent_id}

    # Qdrant hybrid search（dense + sparse RRF 融合）
    search_limit = 20
    import asyncio
    from wrapper.fts5_store import get_fts5
    from wrapper.sparse_vector import get_bm25_encoder
    from qdrant_client.models import Filter as QFilter, FieldCondition, MatchValue
    from qdrant_client.models import Prefetch, FusionQuery, Fusion
    loop = asyncio.get_running_loop()

    try:
        # dense embedding
        _emb_t0 = time.time()
        dense_vec = await loop.run_in_executor(
            None, memory.embedding_model.embed, req.query)
        _emb_ms = int((time.time() - _emb_t0) * 1000)
        logger.info("📊 embedding: items=1 dim=%d model=%s ms=%d",
                     len(dense_vec) if dense_vec else 0,
                     getattr(memory.embedding_model, 'model', 'unknown'),
                     _emb_ms)

        # sparse BM25
        sparse_enc = get_bm25_encoder()
        sparse_vec = sparse_enc.encode_query(req.query)

        # Qdrant filter — 只匹配 user_id，agent_id 可选
        # 历史数据部分 agent_id=N/A，must 过滤会漏掉这些数据
        qfilt = QFilter(must=[
            FieldCondition(key="user_id", match=MatchValue(value=user_id)),
        ])

        # hybrid search: dense + sparse → RRF fusion
        qc = memory.vector_store.client
        hybrid_result = await loop.run_in_executor(
            None,
            lambda: qc.query_points(
                collection_name=getattr(memory, "collection_name", "mem0"),
                query=FusionQuery(fusion=Fusion.RRF),
                prefetch=[
                    Prefetch(query=dense_vec, using="", limit=search_limit),
                    Prefetch(query=sparse_vec, using="bm25", limit=search_limit),
                ],
                limit=search_limit,
                query_filter=qfilt,
                with_payload=["data", "metadata", "tags", "hash",
                              "created_at", "updated_at", "user_id", "agent_id",
                              "attributed_to", "search_count", "last_accessed_at"],
            ))

        # 转为统一格式
        results = []
        for pt in hybrid_result.points:
            payload = pt.payload or {}
            results.append({
                "id": pt.id,
                "memory": payload.get("data", ""),
                "score": pt.score,
                "metadata": {k: v for k, v in payload.items()
                             if k not in ("data", "text_lemmatized", "hash",
                                          "created_at", "updated_at")},
                "hash": payload.get("hash", ""),
                "created_at": payload.get("created_at"),
                "updated_at": payload.get("updated_at"),
                "user_id": payload.get("user_id", ""),
                "agent_id": payload.get("agent_id", ""),
                "attributed_to": payload.get("attributed_to", ""),
            })

        logger.info("🔍 search hybrid: results=%d", len(results))
    except Exception as e:
        logger.warning("hybrid search 失败，降级到 FTS5: %s", e)
        results = []

    # FTS5 补充 snippet（高亮）
    try:
        fts5 = get_fts5()
        _fts5_t0 = time.time()
        fts5_results = await loop.run_in_executor(
            None, fts5.search, req.query, user_id, search_limit, True)
        _fts5_ms = int((time.time() - _fts5_t0) * 1000)
        logger.info("📊 fts5_snippet: results=%d ms=%d", len(fts5_results), _fts5_ms)
        snippet_map = {r["memory_id"]: r for r in fts5_results}
        for r in results:
            fts = snippet_map.get(r["id"])
            if fts:
                if fts.get("snippet"):
                    r["snippet"] = fts["snippet"]
    except Exception as e:
        logger.debug("FTS5 snippet 失败: %s", e)

    # 工作记忆注入：作为 high-priority 候选（权重可配置）
    try:
        wm_cfg = {}
        try:
            from security.utils import get_config
            wm_cfg = get_config().get("working_memory", {})
        except Exception:
            logger.debug("working_memory config load failed", exc_info=True)
        if wm_cfg.get("enabled", True):
            wm_items = await loop.run_in_executor(None, working_memory.list_items, user_id)
            wm_weight = wm_cfg.get("injection_weight", 1.5)
            wm_ttl = wm_cfg.get("default_ttl_days", 90)
            existing_contents = {r.get("memory", "").strip() for r in results if r.get("memory")}
            wm_injected = 0
            for item in wm_items:
                wm_content = item.get("content", "").strip()
                if not wm_content or wm_content in existing_contents:
                    continue
                wm_id = item.get("memory_id", f"wm-{item.get('id', '')}")
                results.append({
                    "id": wm_id,
                    "memory": wm_content,
                    "score": wm_weight,
                    "metadata": {"source": "working_memory", "original_memory_id": item.get("memory_id", "")},
                    "hash": "",
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("accessed_at"),
                    "user_id": user_id,
                    "agent_id": "",
                    "attributed_to": "",
                })
                existing_contents.add(wm_content)
                # 刷新 TTL
                try:
                    await loop.run_in_executor(None, working_memory.touch, item["memory_id"], wm_ttl)
                except Exception:
                    logger.debug("working_memory touch failed", exc_info=True)
                wm_injected += 1
            if wm_injected:
                logger.info("🧠 working_memory: 注入 %d 条候选 (weight=%.1f)", wm_injected, wm_weight)
    except Exception as e:
        logger.debug("working_memory 注入失败: %s", e)

    # 内容去重：工作记忆结果与 Qdrant 结果按 content 去重
    _seen_content = set()
    _deduped = []
    for r in results:
        _key = r.get("memory", "").strip()
        if not _key:
            _deduped.append(r)
            continue
        if _key not in _seen_content:
            _seen_content.add(_key)
            _deduped.append(r)
    results = _deduped

    # 过滤软删除的记忆（metadata.deleted_at 存在则跳过）
    results = [
        r for r in results
        if not (isinstance(r.get("metadata"), dict) and r["metadata"].get("deleted_at"))
    ]

    # 过滤已归档的记忆（矛盾消解标记 archived=true 的旧记忆不返回）
    if not req.include_archived:
        results = [
            r for r in results
            if not (isinstance(r.get("metadata"), dict) and r["metadata"].get("archived"))
        ]

    # 时间窗口过滤
    if req.before or req.after:
        results = _filter_by_time(results, req.before, req.after)

    # tags 过滤
    if req.tags:
        tag_set = set(t.lower() for t in req.tags)
        if req.tags_match == "all":
            results = [
                r for r in results
                if tag_set <= set(t.lower() for t in (r.get("metadata", {}).get("tags") or []))
            ]
        else:  # "any"
            results = [
                r for r in results
                if tag_set & set(t.lower() for t in (r.get("metadata", {}).get("tags") or []))
            ]

    # 5维打分
    try:
        from wrapper.mem0_runtime import load_config
        config = load_config()
        results = score_and_rank(req.query, results, limit=req.limit, config=config)
    except Exception as e:
        logger.warning("scoring 失败: %s", e)

    # salience boost（排序前：让高频记忆排更前）
    try:
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, boost_salience_for_results, results)
    except Exception as e:
        logger.warning("salience boost 失败: %s", e)

    # rerank
    if req.rerank and results:
        try:
            from wrapper.mem0_runtime import rerank as do_rerank, load_config
            config = load_config()
            docs = [r.get("memory", "") for r in results]
            loop = asyncio.get_running_loop()
            _rr_t0 = time.time()
            _scores_before = [(r.get("id"), r.get("score", 0)) for r in results[:5]]
            rerank_results = await loop.run_in_executor(
                None,
                lambda: do_rerank(req.query, docs, top_n=req.limit, config=config),
            )
            _rr_ms = int((time.time() - _rr_t0) * 1000)
            if rerank_results:
                # 融合 rerank 分数
                rerank_weight = config.get("scoring", {}).get("rerank_weight", 0.4)
                for rr in rerank_results:
                    idx = rr.get("index", 0)
                    if 0 <= idx < len(results):
                        base = results[idx].get("score", 0) or 0
                        rerank_s = rr.get("relevance_score", 0)
                        heat = results[idx].get("heat", 0.5)
                        salience_weight = config.get("scoring", {}).get("salience_weight", 0.15)
                        results[idx]["score"] = (
                            base * (1 - rerank_weight)
                            + rerank_s * rerank_weight
                            + heat * salience_weight
                        )
                        results[idx]["rerank_score"] = rerank_s
            _scores_after = [(r.get("id"), r.get("score", 0)) for r in results[:5]]
            logger.info("📊 rerank: docs=%d returned=%d ms=%d model=%s scores_before=%s scores_after=%s",
                         len(docs), len(rerank_results or []), _rr_ms,
                         config.get("reranker", {}).get("model", "unknown"),
                         [(s[0], round(s[1], 4)) for s in _scores_before],
                         [(s[0], round(s[1], 4)) for s in _scores_after])
            results.sort(key=lambda x: x.get("score", 0), reverse=True)
            results = results[:req.limit]
        except Exception as e:
            logger.debug("rerank 失败（降级）: %s", e)
            DegradationTracker.record_degradation("rerank", str(e))
    else:
        # 无 rerank 时仍按 score+heat 排序
        for r in results:
            heat = r.get("heat", 0.5)
            r["score"] = r.get("score", 0) + heat * 0.15
        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        results = results[:req.limit]


    # 收集需要更新的记忆 ID
    vector_memory_ids = [r["id"] for r in results if r.get("id") ]
    if vector_memory_ids:
        await _update_usage_stats_sync(memory, vector_memory_ids)

    elapsed_ms = int((time.time() - start) * 1000)
    logger.debug("🔍 search: results=%d, elapsed=%dms", len(results), elapsed_ms)
    # 召回侧注入边界：给每个 memory 加标记，防止存储型 prompt 注入
    for r in results:
        mem = r.get("memory")
        if mem and not mem.startswith("[MEMORY-DATA]"):
            r["memory"] = f"[MEMORY-DATA]{mem}[/MEMORY-DATA]"
    return {
        "results": results,
        "count": len(results),
        "elapsed_ms": elapsed_ms,
    }


async def _qdrant_retry(coro_factory, *, retries: int = 3, base_delay: float = 0.5):
    """Qdrant 写入重试：指数退避，最多 retries 次。coro_factory 每次调用返回新的 coroutine。"""
    last_exc = None
    for attempt in range(retries):
        try:
            return await coro_factory()
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning("Qdrant 操作失败(第%d次)，%0.1fs 后重试: %s", attempt + 1, delay, e)
                await asyncio.sleep(delay)
    raise last_exc


@app.post("/delete", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def delete_memory(req: DeleteRequest, request: Request):
    """软删除记忆。搜索时过滤，数据仍保留可恢复。"""
    async with _endpoint_semaphore:
        return await _delete_memory_impl(req, request)


async def _delete_memory_impl(req: DeleteRequest, request: Request):
    import re
    from datetime import datetime, timezone

    logger.info("🗑️ delete: memory_id=%s", req.memory_id)
    
    # 1. 格式校验
    if not req.memory_id or not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', req.memory_id, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Invalid memory_id format (must be UUID)")
    
    memory = get_memory()
    
    # 2. 软删除：更新 metadata 标记 deleted_at
    try:
        await memory.update(
            req.memory_id,
            text=None,  # 不改内容，只改 metadata
            metadata={"deleted_at": datetime.now(timezone.utc).isoformat()},
        )
    except Exception as e:
        logger.warning("soft delete failed: %s", e)
        raise HTTPException(status_code=400, detail=f"Delete failed: {e}")

    # 3. tags hook：清空 tags（Qdrant set_payload，同步调用需放线程池）
    loop = asyncio.get_running_loop()
    try:
        from wrapper.tags_hook import on_delete
        await loop.run_in_executor(None, on_delete, memory, req.memory_id)
    except Exception as e:
        logger.debug("tags_hook on_delete 失败: %s", e)

    return {"status": "ok", "memory_id": req.memory_id, "action": "soft_deleted", "confirm_token": _generate_delete_token(req.memory_id, user_id=req.user_id if hasattr(req, "user_id") else None, api_key=request.headers.get("X-API-Key")), "confirm_expires_in": _DELETE_CONFIRM_TTL}


@app.post("/delete/confirm", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def delete_memory_confirm(req: DeleteRequest, request: Request):
    """硬删除记忆（需带 confirm_token，5分钟内有效）。"""
    import re
    
    if not req.confirm_token:
        raise HTTPException(status_code=400, detail="confirm_token required")
    
    confirmed_memory_id = _verify_delete_token(req.confirm_token, api_key=request.headers.get("X-API-Key"))
    if not confirmed_memory_id or confirmed_memory_id != req.memory_id:
        raise HTTPException(status_code=400, detail="Invalid or expired confirm_token")
    
    # 1. 格式校验
    if not req.memory_id or not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', req.memory_id, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Invalid memory_id format (must be UUID)")
    
    memory = get_memory()
    
    # 2. mem0 删除（Qdrant，带重试）
    try:
        await _qdrant_retry(lambda: memory.delete(req.memory_id))
    except Exception as e:
        logger.warning("hard delete failed: %s", e)
        raise HTTPException(status_code=400, detail=f"Delete failed: {e}")
    
    # 3. salience 清理（异步化）
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, salience_delete, req.memory_id)
    except Exception as e:
        logger.warning("salience delete 失败: %s", e)

    # 3.5. working_memory 清理（联动）
    try:
        await loop.run_in_executor(None, working_memory.delete_by_memory_id, req.memory_id)
    except Exception as e:
        logger.warning("working_memory delete 失败: %s", e)

    # 5. FTS5 清理（异步化）
    try:
        from wrapper.fts5_store import get_fts5
        await loop.run_in_executor(None, get_fts5().delete, req.memory_id)
    except Exception as e:
        logger.warning("FTS5 delete 失败: %s", e)

    # 6. version_tracker 清理
    try:
        from wrapper import version_tracker
        await loop.run_in_executor(None, version_tracker.cleanup, req.memory_id)
    except Exception as e:
        logger.warning("version_tracker cleanup 失败: %s", e)

    return {"status": "ok", "memory_id": req.memory_id, "action": "hard_deleted"}



@app.post("/delete/cancel", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def delete_memory_cancel(req: DeleteRequest, request: Request):
    """撤销待确认的删除操作。"""
    if not req.confirm_token:
        raise HTTPException(status_code=400, detail="confirm_token required")
    
    ok = _cancel_delete_token(req.confirm_token)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid or already used/cancelled token")
    
    # 恢复 deleted_at（取消软删除）
    try:
        memory = get_memory()
        await memory.update(req.memory_id, metadata={"deleted_at": None})
    except Exception as e:
        logger.warning("cancel恢复deleted_at失败: %s", e)
    
    return {"status": "ok", "memory_id": req.memory_id, "action": "cancelled"}


@app.post("/update", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def update_memory(req: UpdateRequest, request: Request):
    """更新记忆内容（Qdrant 同步）。

    安全链路：注入防御 → PII 脱敏 → 更新
    """
    async with _endpoint_semaphore:
        return await _update_memory_impl(req, request)


async def _update_memory_impl(req: UpdateRequest, request: Request):
    from security.pipeline import redact_pii
    from security.injection_guard import validate_memory_content

    memory = get_memory()
    logger.info("📝 update: memory_id=%s, content_len=%d", req.memory_id, len(req.content) if req.content else 0)

    # 安全检查：注入防御
    is_valid, cleaned_content, reject_reason = validate_memory_content(req.content)
    if not is_valid or not cleaned_content:
        raise HTTPException(
            status_code=400,
            detail=f"Content rejected: {reject_reason or 'empty content'}",
        )

    # PII 脱敏
    cleaned_content = redact_pii(cleaned_content)

    loop = asyncio.get_running_loop()
    try:
        # 0. 单次 GET：版本追踪 + update_count（避免重复请求）
        existing_meta = {}
        old_content = ""
        try:
            old_item = await memory.get(req.memory_id)
            if old_item:
                old_content = old_item.get("memory", "")
                existing_meta = old_item.get("metadata") or {}
                await loop.run_in_executor(
                    None, version_tracker.save_version,
                    req.memory_id, old_content, existing_meta, "update",
                )
        except Exception as e:
            logger.debug("version_tracker save 失败: %s", e)

        # 1. 增量更新 update_count
        current_update_count = existing_meta.get("update_count", 0)
        if isinstance(current_update_count, (int, float)):
            new_update_count = int(current_update_count) + 1
        else:
            new_update_count = 1
        
        # 合并 metadata
        update_metadata = {
            "update_count": new_update_count,
        }
        if req.metadata:
            update_metadata.update(req.metadata)

        # 2. 更新 Qdrant（带重试）
        await _qdrant_retry(lambda: memory.update(req.memory_id, cleaned_content, metadata=update_metadata))

        # 4. FTS5 双写
        try:
            from wrapper.fts5_store import get_fts5
            _uid = request.headers.get("X-User-ID", "default")
            await loop.run_in_executor(None, get_fts5().write, req.memory_id, cleaned_content, _uid)
        except Exception as e:
            logger.warning("FTS5 update 失败: %s", e)

        # 5. tags hook：更新 tags（Qdrant set_payload，同步调用需放线程池）
        try:
            from wrapper.tags_hook import on_update
            await loop.run_in_executor(None, on_update, memory, req.memory_id, cleaned_content)
        except Exception as e:
            logger.debug("tags_hook on_update 失败: %s", e)
        return {"status": "ok", "memory_id": req.memory_id}
    except Exception as e:
        logger.warning("update memory failed: %s", e)
        raise HTTPException(status_code=500, detail=f"mem0 update failed: {e}")


@app.get("/degradation", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def get_degradation():
    """获取降级状态。"""
    return {
        "degraded": DegradationTracker.get_degraded_components(),
        "details": DegradationTracker.get_degraded_details(),
    }


@app.get("/compensation/dead", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def get_compensation_dead(limit: int = 50):
    """查询补偿队列中已放弃的任务（重试耗尽）。"""
    from security.compensation import dead_stats
    return dead_stats(limit)


@app.get("/stats", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def get_stats():
    """查询 Qdrant 数据量。

    返回各组件的 points/nodes/rels 数量，用于监控和诊断。
    """
    import requests as _req
    from wrapper.mem0_runtime import load_config as _lc

    config = _lc()
    result: Dict[str, Any] = {"qdrant": {}}

    # ── Qdrant ──
    try:
        qdrant_cfg = config.get("mem0", {}).get("vector_store", {}).get("config", {})
        qdrant_url = qdrant_cfg.get("url", "http://127.0.0.1:26333")
        qdrant_key = qdrant_cfg.get("api_key", "")
        headers = {"api-key": qdrant_key} if qdrant_key else {}

        resp = _req.get(f"{qdrant_url}/collections", headers=headers, timeout=5)
        collections = resp.json().get("result", {}).get("collections", [])

        total_points = 0
        for col in collections:
            name = col["name"]
            try:
                r = _req.get(f"{qdrant_url}/collections/{name}", headers=headers, timeout=5)
                pts = r.json().get("result", {}).get("points_count", 0)
                result["qdrant"][name] = pts
                total_points += pts
            except Exception as e:
                logger.debug("Qdrant collection %s 查询失败: %s", name, e)
                result["qdrant"][name] = "error"
        result["qdrant"]["_total"] = total_points
    except Exception as e:
        logger.warning("qdrant stats query failed: %s", e)
        result["qdrant"]["_error"] = str(e)


    return result


@app.post("/expire", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def expire_memories():
    """手动触发过期清理。

    扫描所有记忆，删除已过期的条目（基于 lane TTL 或 expires 标记）。
    同步清理 Qdrant。
    """
    start = time.time()
    loop = asyncio.get_running_loop()
    deleted = await loop.run_in_executor(None, auto_expire.run_expire_cycle)
    elapsed_ms = int((time.time() - start) * 1000)
    return {
        "deleted": deleted,
        "elapsed_ms": elapsed_ms,
    }


@app.get("/expire/status", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def expire_status():
    """查询 auto_expire 后台线程状态。"""
    return {
        "running": auto_expire.is_running(),
    }


@app.post("/consolidate", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def consolidate_memories():
    """手动触发记忆整合。

    查找相似度 >= 85% 的记忆对，合并去重。
    """
    memory = get_memory()
    start = time.time()
    merged = await consolidation.run_consolidation_cycle(memory)
    elapsed_ms = int((time.time() - start) * 1000)
    return {
        "merged": merged,
        "elapsed_ms": elapsed_ms,
    }


@app.get("/consolidate/status", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def consolidate_status():
    """查询 consolidation 后台线程状态。"""
    return {
        "running": consolidation.is_running(),
    }


# ── Core Memory 端点 ──

class CoreMemoryRequest(BaseModel):
    memory_id: str = Field(..., description="记忆 ID")
    category: str = Field(default="general", description="分类")
    importance: float = Field(default=0.5, ge=0.0, le=1.0, description="重要性 0-1")


@app.post("/core-memory/add", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def add_core_memory(req: CoreMemoryRequest):
    """将记忆标记为核心记忆（不会被 auto_expire 清理）。"""
    memory = get_memory()
    # 获取记忆内容
    try:
        results = await memory.search(query="", filters={"memory_id": req.memory_id}, top_k=1)
        items = results.get("results", []) if isinstance(results, dict) else []
        content = items[0].get("memory", "") if items else ""
    except Exception as e:
        logger.debug("core-memory/add 查询失败: %s", e)
        content = ""

    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(
        None, core_memory.add_core_memory,
        req.memory_id, content, req.category, req.importance,
    )
    return {"status": "ok" if ok else "error", "memory_id": req.memory_id}


@app.post("/core-memory/remove", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def remove_core_memory(memory_id: str):
    """移除核心记忆标记。"""
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, core_memory.remove_core_memory, memory_id)
    return {"status": "ok" if ok else "error", "memory_id": memory_id}


@app.get("/core-memory/check/{memory_id}", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def check_core_memory(memory_id: str):
    """检查是否为核心记忆。"""
    loop = asyncio.get_running_loop()
    is_core = await loop.run_in_executor(None, core_memory.is_core_memory, memory_id)
    return {
        "memory_id": memory_id,
        "is_core": is_core,
    }


@app.get("/core-memory/list", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def list_core_memories(category: Optional[str] = None, limit: int = 100):
    """列出核心记忆。"""
    loop = asyncio.get_running_loop()
    memories = await loop.run_in_executor(None, core_memory.list_core_memories, category, limit)
    return {
        "memories": memories,
    }


@app.get("/core-memory/{memory_id}", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def get_core_memory(memory_id: str):
    """获取核心记忆详情。"""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, core_memory.get_core_memory, memory_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Core memory not found")
    return result


@app.put("/core-memory/importance", dependencies=[Depends(verify_api_key)])
async def update_importance(memory_id: str, importance: float):
    """更新核心记忆重要性。"""
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, core_memory.update_importance, memory_id, importance)
    return {"status": "ok" if ok else "error"}


# ── Evolve 端点 ──

@app.post("/evolve", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def evolve_memories():
    """手动触发记忆自进化。

    分析记忆质量，清理低质量记忆，优化整体质量。
    """
    memory = get_memory()
    start = time.time()
    result = await evolve_mem.run_evolve_cycle(memory)
    elapsed_ms = int((time.time() - start) * 1000)
    result["elapsed_ms"] = elapsed_ms
    return result


@app.get("/evolve/status", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def evolve_status():
    """查询 evolve_mem 后台线程状态。"""
    return {
        "running": evolve_mem.is_running(),
    }


@app.get("/evolve/quality", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def memory_quality():
    """分析当前记忆质量。"""
    memory = get_memory()
    return await evolve_mem.process_memory_quality(memory)


# ── Reflect 端点 ──

@app.post("/reflect", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def reflect_memory_system():
    """手动触发系统反思。

    分析记忆系统健康状态，生成改进建议。
    """
    memory = get_memory()
    start = time.time()
    result = await reflect.run_reflect_cycle(memory)
    elapsed_ms = int((time.time() - start) * 1000)
    result["elapsed_ms"] = elapsed_ms
    return result


@app.get("/reflect/status", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def reflect_status():
    """查询 reflect 后台线程状态。"""
    return {
        "running": reflect.is_running(),
    }


@app.get("/reflect/health")
async def system_health():
    """获取系统健康状态。"""
    memory = get_memory()
    return reflect.analyze_system_health(memory)


@app.get("/reflect/logs", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def reflect_logs(limit: int = 10):
    """列出最近的反思日志。"""
    return {
        "logs": reflect.list_reflect_logs(limit),
    }


# ── Version Tracker 端点 ──

@app.get("/versions/stats", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def version_stats():
    """查询版本追踪统计。"""
    loop = asyncio.get_running_loop()
    total = await loop.run_in_executor(None, version_tracker.get_total_versions)
    return {
        "total_versions": total,
    }


@app.get("/versions/{memory_id}", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def get_versions(memory_id: str, limit: int = 20):
    """查询记忆的版本历史（最新在前）。"""
    loop = asyncio.get_running_loop()
    versions = await loop.run_in_executor(None, version_tracker.get_versions, memory_id, limit)
    count = await loop.run_in_executor(None, version_tracker.get_version_count, memory_id)
    return {
        "memory_id": memory_id,
        "versions": versions,
        "total": count,
    }


class RollbackRequest(BaseModel):
    version: int = Field(..., description="要回滚到的版本号")


@app.post("/versions/{memory_id}/rollback", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def rollback_version(memory_id: str, req: RollbackRequest):
    """回滚到指定版本。

    流程：保存当前内容为新版本 → 用旧版本内容覆盖当前记忆
    """
    import re
    if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', memory_id, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Invalid memory_id format")

    # 1. 获取目标版本内容
    loop = asyncio.get_running_loop()
    target = await loop.run_in_executor(None, version_tracker.get_version_content, memory_id, req.version)
    if not target:
        raise HTTPException(status_code=404, detail=f"Version {req.version} not found")

    memory = get_memory()

    # 2. 获取当前内容（保存为新版本）
    try:
        current = await memory.get(memory_id)
        if current:
            current_content = current.get("memory", "")
            current_meta = current.get("metadata") or {}
            await loop.run_in_executor(None, version_tracker.save_version, memory_id, current_content, current_meta, "pre-rollback")
    except Exception as e:
        logger.warning("rollback: 保存当前版本失败: %s", e)

    # 3. 用旧版本内容覆盖
    try:
        await memory.update(memory_id, target["content"])

        return {
            "status": "ok",
            "memory_id": memory_id,
            "rolled_back_to": req.version,
            "restored_content": target["content"],
        }
    except Exception as e:
        logger.warning("rollback failed: %s", e)
        raise HTTPException(status_code=400, detail=f"Rollback failed: {e}")


# ── Hot Archive 端点 ──

@app.get("/archive/candidates", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def archive_candidates():
    """查询热知识候选（满足阈值但尚未归档的记忆）。"""
    loop = asyncio.get_running_loop()
    candidates = await loop.run_in_executor(None, hot_archive.find_hot_candidates)
    return {
        "candidates": candidates,
        "count": len(candidates),
    }


@app.post("/archive/run", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def archive_run():
    """手动触发热知识归档。"""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, hot_archive.run_archive_cycle)
    return result


@app.get("/archive/status", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def archive_status():
    """查询 hot_archive 后台线程状态。"""
    return {
        "running": hot_archive.is_running(),
    }


# ── Reconcile 端点 ──

@app.post("/reconcile", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def run_reconcile():
    """手动触发三库对账检查。

    比对 Qdrant / FTS5 / salience 三端 memory_id 一致性。
    策略：宁可漏删不要误删。
    """
    import asyncio
    loop = asyncio.get_running_loop()
    start = time.time()
    result = await loop.run_in_executor(None, reconcile.reconcile_all)
    result["elapsed_ms"] = int((time.time() - start) * 1000)
    return result


@app.get("/reconcile/stats", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def reconcile_stats():
    """查询三库统计信息 + 最近一次对账结果。"""
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, reconcile.get_stats)


# ── Working Memory 端点 ──

class WorkingMemoryListRequest(BaseModel):
    user_id: str = Field(..., description="用户 ID")


class WorkingMemoryClearRequest(BaseModel):
    user_id: Optional[str] = Field(default=None, description="用户 ID（为空则清空全部）")


@app.post("/working-memory/list", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def list_working_memory(req: WorkingMemoryListRequest, request: Request):
    """获取用户的工作记忆列表。"""
    user_id = request.headers.get("X-User-ID") or req.user_id
    loop = asyncio.get_running_loop()
    items = await loop.run_in_executor(None, working_memory.list_items, user_id)
    return {"items": items, "count": len(items), "user_id": user_id}


@app.post("/working-memory/clear", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def clear_working_memory(req: WorkingMemoryClearRequest, request: Request):
    """清空工作记忆。user_id 为空则清空全部。"""
    user_id = request.headers.get("X-User-ID") or req.user_id
    loop = asyncio.get_running_loop()
    deleted = await loop.run_in_executor(None, working_memory.clear, user_id)
    return {"status": "ok", "deleted": deleted}


# ═══════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════

def _filter_by_time(results: list, before: Optional[str], after: Optional[str]) -> list:
    """按时间窗口过滤结果。"""
    from datetime import datetime
    filtered = []
    for r in results:
        created_at = r.get("created_at") or r.get("metadata", {}).get("created_at")
        if not created_at:
            filtered.append(r)
            continue
        try:
            if isinstance(created_at, (int, float)):
                ts = datetime.fromtimestamp(created_at)
            else:
                ts = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if before:
                before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
                if ts > before_dt:
                    continue
            if after:
                after_dt = datetime.fromisoformat(after.replace("Z", "+00:00"))
                if ts < after_dt:
                    continue
            filtered.append(r)
        except Exception as e:
            logger.warning("time filter parse failed: %s", e)
            filtered.append(r)
    return filtered


# ═══════════════════════════════════════════════════
# 统一 API 入口
# ═══════════════════════════════════════════════════

@app.post("/api", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
async def unified_endpoint(req: UnifiedRequest, request: Request):
    """统一 API 入口。所有操作通过 action 字段路由。

    Header 传递身份信息（X-User-ID, X-Agent-ID 等），
    body 传递业务参数（action + params）。
    旧端点仍可直接调用，此端点为推荐方式。
    """
    identity = _extract_identity(request)
    api_key = request.headers.get("X-API-Key", "anonymous")

    action = req.action
    params = req.params

    logger.info(
        "📨 /api action=%s user=%s agent=%s source=%s session=%s",
        action,
        identity["user_id"] or "(from body)",
        identity["agent_id"] or "(from body)",
        identity["source"],
        identity["session_id"],
    )

    # ── add ──
    if action == "add":
        add_req = AddRequest(
            messages=params.get("messages", ""),
            user_id=identity["user_id"] or params.get("user_id"),
            agent_id=identity["agent_id"] or params.get("agent_id"),
            metadata=params.get("metadata"),
            expiration_date=params.get("expiration_date"),
            infer=params.get("infer", False),
        )
        return await add_memory(add_req, request)

    # ── search ──
    elif action == "search":
        search_req = SearchRequest(
            query=params.get("query", ""),
            user_id=identity["user_id"] or params.get("user_id"),
            agent_id=identity["agent_id"] or params.get("agent_id"),
            limit=params.get("limit", 10),
            rerank=params.get("rerank", True),
            before=params.get("before"),
            after=params.get("after"),
            include_archived=params.get("include_archived", False),
        )
        return await search_memory(search_req, request)

    # ── delete ──
    elif action == "delete":
        delete_req = DeleteRequest(
            memory_id=params.get("memory_id", ""),
            confirm_token=params.get("confirm_token"),
        )
        return await delete_memory(delete_req, request)

    # ── update ──
    elif action == "update":
        update_req = UpdateRequest(
            memory_id=params.get("memory_id", ""),
            content=params.get("content", ""),
            metadata=params.get("metadata"),
        )
        return await update_memory(update_req, request)

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action: {action}. Valid: add, search, delete, update",
        )


# ═══════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    config = load_config()
    server = config.get("server", {})
    host = server.get("host", "127.0.0.1")
    port = server.get("port", 28768)
    logger.info("启动服务: %s:%d", host, port)

    # uvicorn 日志格式：带时间戳，与 httpx/mem0x 统一
    _log_fmt = "%(asctime)s [%(name)s] %(levelname)s %(message)s"
    _log_datefmt = "%H:%M:%S"
    uvicorn_log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": _log_fmt, "datefmt": _log_datefmt},
            "access": {"format": _log_fmt, "datefmt": _log_datefmt},
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
    }
    uvicorn.run(app, host=host, port=port, log_level="info", log_config=uvicorn_log_config)
