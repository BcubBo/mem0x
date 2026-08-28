"""reconcile — 三库对账检查（Qdrant / FTS5 / salience）

每 6h 运行一次，比对三端 memory_id 集合一致性。
策略：宁可漏删不要误删。
- Qdrant 已标 deleted + FTS5/salience 仍有记录 → 清理孤儿
- Qdrant 存在但 FTS5 缺失 → 告警（可配置自动回填）
- FTS5 存在但 Qdrant 不存在 → 告警（可能是孤儿索引）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("mem0x.reconcile")

# ── 默认配置 ──
DEFAULT_INTERVAL = 6 * 3600  # 6 小时
QDRANT_BATCH_SIZE = 500

# ── 全局状态 ──
_running = False
_thread: Optional[threading.Thread] = None
_last_run: Optional[float] = None
_last_result: Optional[Dict[str, Any]] = None


def _load_config() -> dict:
    """从 config.json 读取 reconcile 配置。"""
    config_path = os.environ.get("MEM0X_CONFIG", "config.json")
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        return cfg.get("reconcile", {})
    except Exception as e:
        logger.warning("load_config: %s", e)
        return {}


def _fetch_qdrant_content(memory_ids: Set[str]) -> Dict[str, Dict[str, str]]:
    """从 Qdrant 批量获取指定 memory_id 的 content 和 user_id。

    Returns:
        {memory_id: {"content": ..., "user_id": ...}}
    """
    from wrapper.mem0_runtime import get_memory
    mem = get_memory()
    if mem is None:
        return {}

    try:
        client = mem.vector_store.client
        collection = getattr(mem.vector_store, "collection_name", "mem0")
    except AttributeError:
        return {}

    result: Dict[str, Dict[str, str]] = {}
    id_list = list(memory_ids)

    # 分批 retrieve（Qdrant retrieve 上限约 1000）
    for i in range(0, len(id_list), QDRANT_BATCH_SIZE):
        batch = id_list[i:i + QDRANT_BATCH_SIZE]
        try:
            points = client.retrieve(
                collection_name=collection,
                ids=batch,
                with_payload=True,
                with_vectors=False,
            )
            for pt in points:
                payload = pt.payload or {}
                content = payload.get("data", "") or payload.get("memory", "")
                user_id = payload.get("user_id", "")
                if content:
                    result[str(pt.id)] = {"content": content, "user_id": user_id}
        except Exception as e:
            logger.warning("reconcile: Qdrant retrieve 批次失败: %s", e)

    return result


# ═══════════════════════════════════════════════════
# 数据收集
# ═══════════════════════════════════════════════════

def _collect_qdrant_ids() -> tuple[Set[str], Set[str]]:
    """从 Qdrant 收集 (active_ids, deleted_ids)。

    使用 fetch_all.iter_batches 同步版本直接 scroll。
    返回两组 memory_id 集合。
    """
    from wrapper.mem0_runtime import get_memory
    mem = get_memory()
    if mem is None:
        logger.warning("reconcile: mem0 实例不可用")
        return set(), set()

    try:
        client = mem.vector_store.client
        collection = getattr(mem.vector_store, "collection_name", "mem0")
    except AttributeError:
        logger.warning("reconcile: 无法获取 Qdrant client")
        return set(), set()

    active_ids: Set[str] = set()
    deleted_ids: Set[str] = set()
    offset = None

    while True:
        try:
            points, next_offset = client.scroll(
                collection_name=collection,
                limit=QDRANT_BATCH_SIZE,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            logger.warning("reconcile: Qdrant scroll 失败: %s", e)
            break

        if not points:
            break

        for pt in points:
            mid = str(pt.id)
            payload = pt.payload or {}
            if payload.get("deleted_at"):
                deleted_ids.add(mid)
            else:
                active_ids.add(mid)

        if next_offset is None or next_offset == offset:
            break
        offset = next_offset

    logger.debug("reconcile: Qdrant active=%d, deleted=%d", len(active_ids), len(deleted_ids))
    return active_ids, deleted_ids


def _collect_fts5_ids() -> Set[str]:
    """从 FTS5 meta 表收集所有 memory_id。"""
    try:
        from wrapper.fts5_store import get_fts5
        fts5 = get_fts5()
    except Exception as e:
        logger.warning("reconcile: FTS5 不可用: %s", e)
        return set()

    ids: Set[str] = set()
    try:
        rows = fts5._conn.execute("SELECT memory_id FROM fts5_meta").fetchall()
        for row in rows:
            ids.add(row[0])
    except Exception as e:
        logger.warning("reconcile: FTS5 查询失败: %s", e)

    logger.debug("reconcile: FTS5 count=%d", len(ids))
    return ids


def _collect_salience_ids() -> Set[str]:
    """从 salience 表收集所有 memory_id。"""
    try:
        from wrapper.salience import _get_db
        conn = _get_db()
    except Exception as e:
        logger.warning("reconcile: salience 不可用: %s", e)
        return set()

    ids: Set[str] = set()
    try:
        rows = conn.execute("SELECT memory_id FROM salience").fetchall()
        for row in rows:
            ids.add(row["memory_id"])
    except Exception as e:
        logger.warning("reconcile: salience 查询失败: %s", e)
    finally:
        conn.close()

    logger.debug("reconcile: salience count=%d", len(ids))
    return ids


# ═══════════════════════════════════════════════════
# 对账逻辑
# ═══════════════════════════════════════════════════

def reconcile_all() -> Dict[str, Any]:
    """执行三库对账检查。

    策略：宁可漏删不要误删。
    - Qdrant 已标 deleted + FTS5/salience 仍有记录 → 清理孤儿
    - Qdrant 存在但 FTS5 缺失 → 告警（可配置 auto_backfill_fts5 自动回填）
    - FTS5 存在但 Qdrant 不存在 → 告警（孤儿索引）
    - salience 存在但 Qdrant 不存在 → 告警

    Returns:
        对账结果摘要
    """
    global _last_run, _last_result
    start = time.time()
    logger.info("reconcile: 开始对账检查")

    # 1. 收集三端 ID 集合
    qdrant_active, qdrant_deleted = _collect_qdrant_ids()
    fts5_ids = _collect_fts5_ids()
    salience_ids = _collect_salience_ids()

    # 2. 计算差异
    # 场景 A：Qdrant 已标 deleted 但 FTS5/salience 仍有 → 清理孤儿
    deleted_in_fts5 = qdrant_deleted & fts5_ids
    deleted_in_salience = qdrant_deleted & salience_ids

    # 场景 B：Qdrant active 但 FTS5 缺失 → 可配置自动回填
    missing_fts5 = qdrant_active - fts5_ids

    # 场景 C：FTS5 存在但 Qdrant 不存在（active+deleted 都没有）→ 告警
    all_qdrant = qdrant_active | qdrant_deleted
    orphan_fts5 = fts5_ids - all_qdrant

    # 场景 D：salience 存在但 Qdrant 不存在 → 告警
    orphan_salience = salience_ids - all_qdrant

    # 3. 执行清理（只清理场景 A：deleted 记忆的孤儿）
    cleaned_fts5 = 0
    cleaned_salience = 0

    if deleted_in_fts5:
        try:
            from wrapper.fts5_store import get_fts5
            fts5 = get_fts5()
            for mid in deleted_in_fts5:
                try:
                    fts5.delete(mid)
                    cleaned_fts5 += 1
                except Exception as e:
                    logger.debug("reconcile: FTS5 清理孤儿失败 %s: %s", mid[:12], e)
        except Exception as e:
            logger.warning("reconcile: FTS5 清理批次失败: %s", e)

    if deleted_in_salience:
        try:
            from wrapper.salience import delete as sal_delete
            for mid in deleted_in_salience:
                try:
                    sal_delete(mid)
                    cleaned_salience += 1
                except Exception as e:
                    logger.debug("reconcile: salience 清理孤儿失败 %s: %s", mid[:12], e)
        except Exception as e:
            logger.warning("reconcile: salience 清理批次失败: %s", e)

    # 场景 B 处理：可配置的 FTS5 自动回填
    backfilled_fts5 = 0
    reconcile_cfg = _load_config()
    auto_backfill = reconcile_cfg.get("auto_backfill_fts5", False)

    if missing_fts5 and auto_backfill:
        logger.info("reconcile: 自动回填 %d 条缺失 FTS5 记录", len(missing_fts5))
        try:
            from wrapper.fts5_store import get_fts5
            fts5 = get_fts5()
            records = _fetch_qdrant_content(missing_fts5)
            for mid, info in records.items():
                try:
                    fts5.write(mid, info["content"], info["user_id"])
                    backfilled_fts5 += 1
                except Exception as e:
                    logger.debug("reconcile: FTS5 回填失败 %s: %s", mid[:12], e)
            logger.info("reconcile: FTS5 自动回填完成 %d/%d", backfilled_fts5, len(missing_fts5))
        except Exception as e:
            logger.warning("reconcile: FTS5 自动回填批次失败: %s", e)

    elapsed_ms = int((time.time() - start) * 1000)

    # 4. 构建结果
    result = {
        "timestamp": time.time(),
        "elapsed_ms": elapsed_ms,
        "counts": {
            "qdrant_active": len(qdrant_active),
            "qdrant_deleted": len(qdrant_deleted),
            "fts5": len(fts5_ids),
            "salience": len(salience_ids),
        },
        "orphan_cleaned": {
            "fts5": cleaned_fts5,
            "salience": cleaned_salience,
        },
        "fts5_backfilled": backfilled_fts5,
        "warnings": {
            "missing_fts5": len(missing_fts5),
            "orphan_fts5": len(orphan_fts5),
            "orphan_salience": len(orphan_salience),
        },
        "status": "ok",
    }

    # 有告警时记录详情（最多列 20 个 ID，避免日志爆炸）
    if missing_fts5:
        sample = list(missing_fts5)[:20]
        result["warnings"]["missing_fts5_sample"] = sample
        if auto_backfill:
            logger.info("reconcile: %d 条 FTS5 缺失，自动回填 %d 条",
                        len(missing_fts5), backfilled_fts5)
        else:
            logger.warning("reconcile: %d 条 Qdrant active 记忆在 FTS5 中缺失（示例: %s）",
                           len(missing_fts5), ", ".join(s[:12] for s in sample[:5]))

    if orphan_fts5:
        sample = list(orphan_fts5)[:20]
        result["warnings"]["orphan_fts5_sample"] = sample
        logger.warning("reconcile: %d 条 FTS5 孤儿索引（Qdrant 中不存在）", len(orphan_fts5))

    if orphan_salience:
        sample = list(orphan_salience)[:20]
        result["warnings"]["orphan_salience_sample"] = sample
        logger.warning("reconcile: %d 条 salience 孤儿记录（Qdrant 中不存在）", len(orphan_salience))

    if cleaned_fts5 or cleaned_salience:
        logger.info("reconcile: 清理孤儿 FTS5=%d, salience=%d", cleaned_fts5, cleaned_salience)

    logger.info("reconcile: 对账完成 qdrant=%d/%d fts5=%d salience=%d 耗时=%dms",
                len(qdrant_active), len(qdrant_deleted), len(fts5_ids), len(salience_ids), elapsed_ms)

    _last_run = time.time()
    _last_result = result
    return result


# ═══════════════════════════════════════════════════
# 统计查询
# ═══════════════════════════════════════════════════

def get_stats() -> Dict[str, Any]:
    """获取三库统计信息 + 最近一次对账结果。"""
    qdrant_active, qdrant_deleted = _collect_qdrant_ids()
    fts5_ids = _collect_fts5_ids()
    salience_ids = _collect_salience_ids()

    stats = {
        "qdrant_active": len(qdrant_active),
        "qdrant_deleted": len(qdrant_deleted),
        "fts5": len(fts5_ids),
        "salience": len(salience_ids),
        "last_reconcile_at": _last_run,
        "last_reconcile_result": _last_result,
    }
    return stats


# ═══════════════════════════════════════════════════
# 后台线程
# ═══════════════════════════════════════════════════

def _background_loop(interval: int):
    """后台循环线程。"""
    global _running
    logger.info("reconcile 后台线程启动，间隔 %ds", interval)

    from wrapper.evolve_lock import background_tasks_lock

    # 启动后延迟 60s 再执行首次对账，避免启动风暴
    time.sleep(60)

    while _running:
        try:
            if not background_tasks_lock.acquire(timeout=5):
                logger.debug("reconcile: 等待共享锁超时，跳过本轮")
                time.sleep(interval)
                continue
            try:
                reconcile_all()
            except Exception as e:
                logger.error("reconcile 对账异常: %s", e)
            finally:
                try:
                    background_tasks_lock.release()
                except Exception as e:
                    logger.debug("reconcile: lock release failed: %s", e)
        except Exception as e:
            logger.error("reconcile 循环异常: %s", e)

        time.sleep(interval)

    logger.info("reconcile 后台线程已停止")


def start_reconcile_thread(interval: int = DEFAULT_INTERVAL):
    """启动对账后台线程。"""
    global _running, _thread
    if _running:
        logger.warning("reconcile 已在运行")
        return

    _running = True
    _thread = threading.Thread(
        target=_background_loop,
        args=(interval,),
        daemon=True,
        name="reconcile",
    )
    _thread.start()


def stop():
    """停止后台线程。"""
    global _running
    _running = False


def is_running() -> bool:
    return _running
