"""api_server — bMem0X 独立服务入口

FastAPI HTTP 服务，提供 /add, /search, /delete, /health 端点。
"""
from __future__ import annotations

import logging
import os
import sys
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
logger = logging.getLogger("mem0x")

# ── 确保项目根目录在 sys.path ──
PROJECT_ROOT = str(Path(__file__).resolve().parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

# ── 延迟导入（配置加载后才初始化） ──
from wrapper.mem0_runtime import get_memory, load_config
from wrapper.salience import boost_salience_for_results, register as salience_register, delete as salience_delete
from wrapper.neo4j_hook import get_hook
from wrapper import auto_expire
from wrapper import consolidation
from wrapper import core_memory
from wrapper import evolve_mem
from wrapper import reflect
from wrapper import hot_archive
from wrapper import graph_export
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


class DeleteRequest(BaseModel):
    memory_id: str = Field(..., description="记忆 ID")


class UpdateRequest(BaseModel):
    memory_id: str = Field(..., description="记忆 ID")
    content: str = Field(..., description="新内容")
    metadata: Optional[Dict[str, Any]] = None


# ═══════════════════════════════════════════════════
# Lifespan
# ═══════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化 mem0 + Neo4j + auto_expire，关闭时清理。"""
    logger.info("mem0x 启动中...")
    config = load_config()

    # 加载脱敏名称映射（从 config.json 的 redact_names 字段）
    try:
        from security.pipeline import load_redact_names as _load_pipeline_redact
        _load_pipeline_redact(config)
    except Exception as e:
        logger.debug("pipeline redact_names 加载失败: %s", e)
    try:
        from wrapper.neo4j_hook import load_redact_names as _load_neo4j_redact
        _load_neo4j_redact(config)
    except Exception as e:
        logger.debug("neo4j redact_names 加载失败: %s", e)
    try:
        from wrapper.neo4j_hook import load_known_entities as _load_entities
        _load_entities(config)
    except Exception as e:
        logger.debug("neo4j known_entities 加载失败: %s", e)

    # 初始化 mem0 单例
    try:
        mem = get_memory(config)
        logger.info("mem0 初始化成功")
    except Exception as e:
        logger.error("mem0 初始化失败: %s", e)

    # 初始化 Neo4j
    try:
        hook = get_hook()
        if hook.enabled:
            logger.info("Neo4j 已连接")
        else:
            logger.info("Neo4j 未启用")
    except Exception as e:
        logger.warning("Neo4j 初始化失败: %s", e)

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

    yield

    # 关闭
    auto_expire.stop()
    consolidation.stop()
    evolve_mem.stop()
    reflect.stop()
    hot_archive.stop()
    try:
        hook = get_hook()
        hook.shutdown()
    except Exception:
        pass
    logger.info("mem0x 已关闭")


app = FastAPI(
    title="mem0x",
    description="自托管 AI 记忆增强服务",
    version="0.1.15",
    lifespan=lifespan,
)


# ═══════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════

@app.get("/health")
async def health():
    """健康检查。"""
    from wrapper.mem0_runtime import _memory_instance
    mem_ok = _memory_instance is not None
    neo4j_ok = get_hook().enabled
    degraded = DegradationTracker.get_degraded_components()
    return {
        "status": "ok" if mem_ok else "degraded",
        "mem0": mem_ok,
        "neo4j": neo4j_ok,
        "degraded_components": degraded,
    }


@app.post("/add")
async def add_memory(req: AddRequest, request: Request):
    """安全写入记忆。

    链路：注入防御 → PII脱敏 → 去重 → 矛盾消解 → 语义判重 → 写入
    user_id 优先级：请求头 X-User-ID > 请求体 user_id > 默认 "bo"
    """
    memory = get_memory()
    start = time.time()

    # 从请求头或请求体获取 user_id/agent_id
    user_id = request.headers.get("X-User-ID") or req.user_id or "bo"
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

    logger.info("📥 add: user=%s, agent=%s, content_len=%d, content=%s", user_id, agent_id, len(content), content[:80])

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
    result = safe_add(
        memory, content, filters,
        user_id=user_id, agent_id=agent_id,
        metadata=usage_metadata, expiration_date=req.expiration_date,
        infer=req.infer,
    )

    # 写入后：注册 salience + Neo4j + 版本追踪
    memory_id = result.get("memory_id")
    if memory_id and result.get("action") in ("added", "conflict"):
        try:
            salience_register(memory_id, content_preview=content[:200])
        except Exception as e:
            logger.debug("salience register 失败: %s", e)

        # 版本追踪：保存初始版本
        try:
            version_tracker.save_version(memory_id, content, reason="create")
        except Exception as e:
            logger.debug("version_tracker init 失败: %s", e)

        try:
            hook = get_hook()
            if hook.enabled:
                hook.write(memory_id, content)
        except Exception as e:
            logger.debug("neo4j write 失败: %s", e)

    elapsed_ms = int((time.time() - start) * 1000)
    result["elapsed_ms"] = elapsed_ms
    return result


@app.post("/search")
async def search_memory(req: SearchRequest, request: Request):
    """搜索记忆。

    链路：向量检索 → Neo4j引导查询 → 5维打分 → rerank → salience boost
    user_id 优先级：请求头 X-User-ID > 请求体 user_id > 默认 "bo"
    """
    memory = get_memory()
    start = time.time()

    # 从请求头或请求体获取 user_id/agent_id
    user_id = request.headers.get("X-User-ID") or req.user_id or "bo"
    agent_id = request.headers.get("X-Agent-ID") or req.agent_id or "hermes"

    # 构建 filters（mem0 2.0+ 必须有 user_id/agent_id/run_id 之一）
    filters = {"user_id": user_id, "agent_id": agent_id}

    # 向量检索（缩小候选池，用 top 结果引导 Neo4j）
    search_limit = 20
    try:
        raw = memory.search(req.query, filters=filters, top_k=search_limit)
        results = raw.get("results", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    except Exception as e:
        logger.warning("mem0 search 失败: %s", e)
        results = []

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

    # Neo4j 引导查询：用 query + top-3 结果文本提取实体，引导图谱查询
    neo4j_results = []
    try:
        hook = get_hook()
        if hook.enabled:
            top_texts = [req.query] + [r.get("memory", "") for r in results[:3]]
            neo4j_results = hook.query(req.query, extra_texts=top_texts)
    except Exception as e:
        logger.debug("neo4j query 失败: %s", e)
        DegradationTracker.record_degradation("neo4j", str(e))
    # 5维打分
    try:
        from wrapper.mem0_runtime import load_config
        config = load_config()
        results = score_and_rank(req.query, results, limit=req.limit, config=config)
    except Exception as e:
        logger.debug("scoring 失败: %s", e)

    # salience boost（排序前：让高频记忆排更前）
    try:
        results = boost_salience_for_results(results)
    except Exception as e:
        logger.debug("salience boost 失败: %s", e)

    # rerank
    if req.rerank and results:
        try:
            from wrapper.mem0_runtime import rerank as do_rerank, load_config
            config = load_config()
            docs = [r.get("memory", "") for r in results]
            rerank_results = do_rerank(req.query, docs, top_n=req.limit, config=config)
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


# Neo4j 图谱联想追加（格式化 + 动态score）
    if neo4j_results:
        for nr in neo4j_results:
            # 基于关系数量计算score：base 0.2 + 每个关系+0.05，上限0.6
            relations = nr.get("relations", [])
            if isinstance(relations, str):
                relations = [r.strip() for r in relations.split(",") if r.strip()]
            relation_bonus = min(len(relations) * 0.05, 0.4)
            score = 0.2 + relation_bonus
            
            rel_text = " 关联: " + str(nr.get("relations", "")) if nr.get("relations") else ""
            results.append({
                "id": "neo4j:" + nr["name"],
                "memory": "[" + nr["label"] + "] " + nr["name"] + rel_text,
                "score": round(score, 2),
            })

    # 更新使用维度字段（异步，不阻塞响应）
    async def _update_usage_stats(memory_ids: list):
        """批量更新被搜索记忆的使用维度字段。"""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        for mid in memory_ids:
            if not mid or mid.startswith("neo4j:"):
                continue
            try:
                # 获取现有记忆的 metadata
                existing = memory.get(mid)
                existing_metadata = existing.get("metadata", {}) if existing else {}
                
                # 增量更新 search_count
                current_count = existing_metadata.get("search_count", 0)
                if isinstance(current_count, (int, float)):
                    new_count = int(current_count) + 1
                else:
                    new_count = 1
                
                # 更新 metadata
                memory.update(
                    mid,
                    metadata={
                        "search_count": new_count,
                        "last_accessed_at": now,
                    }
                )
            except Exception as e:
                logger.debug("更新使用维度失败 %s: %s", mid[:16], e)

    # 收集需要更新的记忆 ID（只更新向量结果，不更新 neo4j 结果）
    vector_memory_ids = [r["id"] for r in results if r.get("id") and not r["id"].startswith("neo4j:")]
    if vector_memory_ids:
        import asyncio
        asyncio.create_task(_update_usage_stats(vector_memory_ids))

    elapsed_ms = int((time.time() - start) * 1000)
    logger.info("🔍 search: query=%s, results=%d, elapsed=%dms", req.query[:50], len(results), elapsed_ms)
    return {
        "results": results,
        "count": len(results),
        "elapsed_ms": elapsed_ms,
    }


@app.post("/delete")
async def delete_memory(req: DeleteRequest):
    """删除记忆（级联清理 Qdrant + salience + Neo4j）。

    安全策略：软删除 — 标记 deleted_at，搜索时过滤。
    硬删除需通过 /delete/confirm 端点。
    """
    import re
    from datetime import datetime, timezone
    
    logger.info("🗑️ delete: memory_id=%s", req.memory_id)
    
    # 1. 格式校验
    if not req.memory_id or not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', req.memory_id, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Invalid memory_id format (must be UUID)")
    
    memory = get_memory()
    
    # 2. 软删除：更新 metadata 标记 deleted_at
    try:
        memory.update(
            req.memory_id,
            text=None,  # 不改内容，只改 metadata
            metadata={"deleted_at": datetime.now(timezone.utc).isoformat()},
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Delete failed: {e}")
    
    return {"status": "ok", "memory_id": req.memory_id, "action": "soft_deleted"}


@app.post("/delete/confirm")
async def delete_memory_confirm(req: DeleteRequest):
    """硬删除记忆（级联清理 Qdrant + salience + Neo4j）。

    调用方需显式调用此端点才能真正删除。
    """
    import re
    
    # 1. 格式校验
    if not req.memory_id or not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', req.memory_id, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Invalid memory_id format (must be UUID)")
    
    memory = get_memory()
    
    # 2. mem0 删除（Qdrant）
    try:
        memory.delete(req.memory_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Delete failed: {e}")
    
    # 3. salience 清理
    try:
        salience_delete(req.memory_id)
    except Exception as e:
        logger.debug("salience delete 失败: %s", e)
    
    # 4. Neo4j 清理
    try:
        hook = get_hook()
        if hook.enabled:
            hook.cleanup(req.memory_id)
    except Exception as e:
        logger.debug("neo4j cleanup 失败: %s", e)
    
    return {"status": "ok", "memory_id": req.memory_id, "action": "hard_deleted"}


@app.post("/update")
async def update_memory(req: UpdateRequest):
    """更新记忆内容（Qdrant + Neo4j 双端同步）。

    安全链路：注入防御 → PII 脱敏 → 更新
    """
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

    try:
        # 0. 版本追踪：更新前保存旧版本
        try:
            old_item = memory.get(req.memory_id)
            if old_item:
                old_content = old_item.get("memory", "")
                old_meta = old_item.get("metadata") or {}
                version_tracker.save_version(
                    req.memory_id, old_content, old_meta, reason="update",
                )
        except Exception as e:
            logger.debug("version_tracker save 失败: %s", e)

        # 1. 增量更新 update_count
        existing_meta = {}
        try:
            existing_item = memory.get(req.memory_id)
            if existing_item:
                existing_meta = existing_item.get("metadata") or {}
        except Exception:
            pass
        
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

        # 2. 更新 Qdrant
        memory.update(req.memory_id, cleaned_content, metadata=update_metadata)

        # 3. 同步更新 Neo4j（先删后写）
        try:
            hook = get_hook()
            if hook.enabled:
                hook.cleanup(req.memory_id)
                hook.write(req.memory_id, cleaned_content)
        except Exception as e:
            logger.debug("neo4j update 失败: %s", e)

        return {"status": "ok", "memory_id": req.memory_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"mem0 update failed: {e}")


@app.get("/degradation")
async def get_degradation():
    """获取降级状态。"""
    return {
        "degraded": DegradationTracker.get_degraded_components(),
        "details": DegradationTracker.get_degraded_details(),
    }


@app.get("/stats")
async def get_stats():
    """查询 Qdrant 和 Neo4j 数据量。

    返回各组件的 points/nodes/rels 数量，用于监控和诊断。
    """
    import requests as _req
    from wrapper.mem0_runtime import load_config as _lc

    config = _lc()
    result: Dict[str, Any] = {"qdrant": {}, "neo4j": {}}

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
            except Exception:
                result["qdrant"][name] = "error"
        result["qdrant"]["_total"] = total_points
    except Exception as e:
        result["qdrant"]["_error"] = str(e)

    # ── Neo4j ──
    try:
        hook = get_hook()
        if hook.enabled and hook._driver:
            with hook._driver.session() as session:
                nodes = session.run("MATCH (n) RETURN count(n) as c").single()["c"]
                rels = session.run("MATCH ()-[r]->() RETURN count(r) as c").single()["c"]
            result["neo4j"]["nodes"] = nodes
            result["neo4j"]["relationships"] = rels
        else:
            result["neo4j"]["_error"] = "not connected"
    except Exception as e:
        result["neo4j"]["_error"] = str(e)

    return result


@app.post("/expire")
async def expire_memories():
    """手动触发过期清理。

    扫描所有记忆，删除已过期的条目（基于 lane TTL 或 expires 标记）。
    同步清理 Qdrant + Neo4j。
    """
    memory = get_memory()
    hook = get_hook()
    start = time.time()
    deleted = auto_expire.run_expire_cycle(memory, neo4j_hook=hook)
    elapsed_ms = int((time.time() - start) * 1000)
    return {
        "deleted": deleted,
        "elapsed_ms": elapsed_ms,
    }


@app.get("/expire/status")
async def expire_status():
    """查询 auto_expire 后台线程状态。"""
    return {
        "running": auto_expire.is_running(),
    }


@app.post("/consolidate")
async def consolidate_memories():
    """手动触发记忆整合。

    查找相似度 >= 85% 的记忆对，合并去重。
    """
    memory = get_memory()
    hook = get_hook()
    start = time.time()
    merged = consolidation.run_consolidation_cycle(memory, neo4j_hook=hook)
    elapsed_ms = int((time.time() - start) * 1000)
    return {
        "merged": merged,
        "elapsed_ms": elapsed_ms,
    }


@app.get("/consolidate/status")
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


@app.post("/core-memory/add")
async def add_core_memory(req: CoreMemoryRequest):
    """将记忆标记为核心记忆（不会被 auto_expire 清理）。"""
    memory = get_memory()
    # 获取记忆内容
    try:
        results = memory.search(query="", filters={"memory_id": req.memory_id}, top_k=1)
        items = results.get("results", []) if isinstance(results, dict) else []
        content = items[0].get("memory", "") if items else ""
    except Exception:
        content = ""

    ok = core_memory.add_core_memory(
        req.memory_id, content, req.category, req.importance
    )
    return {"status": "ok" if ok else "error", "memory_id": req.memory_id}


@app.post("/core-memory/remove")
async def remove_core_memory(memory_id: str):
    """移除核心记忆标记。"""
    ok = core_memory.remove_core_memory(memory_id)
    return {"status": "ok" if ok else "error", "memory_id": memory_id}


@app.get("/core-memory/check/{memory_id}")
async def check_core_memory(memory_id: str):
    """检查是否为核心记忆。"""
    return {
        "memory_id": memory_id,
        "is_core": core_memory.is_core_memory(memory_id),
    }


@app.get("/core-memory/list")
async def list_core_memories(category: Optional[str] = None, limit: int = 100):
    """列出核心记忆。"""
    return {
        "memories": core_memory.list_core_memories(category, limit),
    }


@app.get("/core-memory/{memory_id}")
async def get_core_memory(memory_id: str):
    """获取核心记忆详情。"""
    result = core_memory.get_core_memory(memory_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Core memory not found")
    return result


@app.put("/core-memory/importance")
async def update_importance(memory_id: str, importance: float):
    """更新核心记忆重要性。"""
    ok = core_memory.update_importance(memory_id, importance)
    return {"status": "ok" if ok else "error"}


# ── Evolve 端点 ──

@app.post("/evolve")
async def evolve_memories():
    """手动触发记忆自进化。

    分析记忆质量，清理低质量记忆，优化整体质量。
    """
    memory = get_memory()
    hook = get_hook()
    start = time.time()
    result = evolve_mem.run_evolve_cycle(memory, neo4j_hook=hook)
    elapsed_ms = int((time.time() - start) * 1000)
    result["elapsed_ms"] = elapsed_ms
    return result


@app.get("/evolve/status")
async def evolve_status():
    """查询 evolve_mem 后台线程状态。"""
    return {
        "running": evolve_mem.is_running(),
    }


@app.get("/evolve/quality")
async def memory_quality():
    """分析当前记忆质量。"""
    memory = get_memory()
    return evolve_mem.analyze_memory_quality(memory)


# ── Reflect 端点 ──

@app.post("/reflect")
async def reflect_memory_system():
    """手动触发系统反思。

    分析记忆系统健康状态，生成改进建议。
    """
    memory = get_memory()
    start = time.time()
    result = reflect.run_reflect_cycle(memory)
    elapsed_ms = int((time.time() - start) * 1000)
    result["elapsed_ms"] = elapsed_ms
    return result


@app.get("/reflect/status")
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


@app.get("/reflect/logs")
async def reflect_logs(limit: int = 10):
    """列出最近的反思日志。"""
    return {
        "logs": reflect.list_reflect_logs(limit),
    }


# ── Version Tracker 端点 ──

@app.get("/versions/stats")
async def version_stats():
    """查询版本追踪统计。"""
    return {
        "total_versions": version_tracker.get_total_versions(),
    }


@app.get("/versions/{memory_id}")
async def get_versions(memory_id: str, limit: int = 20):
    """查询记忆的版本历史（最新在前）。"""
    versions = version_tracker.get_versions(memory_id, limit)
    count = version_tracker.get_version_count(memory_id)
    return {
        "memory_id": memory_id,
        "versions": versions,
        "total": count,
    }


class RollbackRequest(BaseModel):
    version: int = Field(..., description="要回滚到的版本号")


@app.post("/versions/{memory_id}/rollback")
async def rollback_version(memory_id: str, req: RollbackRequest):
    """回滚到指定版本。

    流程：保存当前内容为新版本 → 用旧版本内容覆盖当前记忆
    """
    import re
    if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', memory_id, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Invalid memory_id format")

    # 1. 获取目标版本内容
    target = version_tracker.get_version_content(memory_id, req.version)
    if not target:
        raise HTTPException(status_code=404, detail=f"Version {req.version} not found")

    memory = get_memory()

    # 2. 获取当前内容（保存为新版本）
    try:
        current = memory.get(memory_id)
        if current:
            current_content = current.get("memory", "")
            current_meta = current.get("metadata") or {}
            version_tracker.save_version(memory_id, current_content, current_meta, reason="pre-rollback")
    except Exception as e:
        logger.debug("rollback: 保存当前版本失败: %s", e)

    # 3. 用旧版本内容覆盖
    try:
        memory.update(memory_id, target["content"])

        # 4. 同步 Neo4j
        try:
            hook = get_hook()
            if hook.enabled:
                hook.cleanup(memory_id)
                hook.write(memory_id, target["content"])
        except Exception as e:
            logger.debug("rollback: Neo4j 同步失败: %s", e)

        return {
            "status": "ok",
            "memory_id": memory_id,
            "rolled_back_to": req.version,
            "restored_content": target["content"],
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Rollback failed: {e}")


# ── Graph Export 端点 ──

@app.get("/graph/export")
async def export_graph(
    limit: int = 200,
    depth: int = 2,
    entity_type: Optional[str] = None,
    center: Optional[str] = None,
):
    """导出知识图谱数据（节点+边）。

    用于 Hermes 搜索增强或前端可视化。
    - 全局导出：不传 center，按连接数排序取 top-N
    - 子图导出：传 center，从中心节点展开 depth 层
    """
    return graph_export.export_graph(
        limit=limit, depth=depth, entity_type=entity_type, center=center,
    )


# ── Hot Archive 端点 ──

@app.get("/archive/candidates")
async def archive_candidates():
    """查询热知识候选（满足阈值但尚未归档的记忆）。"""
    candidates = hot_archive.find_hot_candidates()
    return {
        "candidates": candidates,
        "count": len(candidates),
    }


@app.post("/archive/run")
async def archive_run():
    """手动触发热知识归档。"""
    result = hot_archive.run_archive_cycle()
    return result


@app.get("/archive/status")
async def archive_status():
    """查询 hot_archive 后台线程状态。"""
    return {
        "running": hot_archive.is_running(),
    }


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
        except Exception:
            filtered.append(r)
    return filtered


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

    # uvicorn 日志格式：带时间戳，与 httpx/bMem0X 统一
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
