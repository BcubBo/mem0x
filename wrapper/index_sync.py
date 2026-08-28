"""IndexSync — 跨存储同步层。

当 evolve_mem/consolidation/auto_expire 操作 Qdrant 后，
同步更新 FTS5 全文索引、salience 重要性表、version_tracker 版本历史。

确保 Qdrant、FTS5、SQLite 三端数据一致。
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger("mem0x.index_sync")


def _get_fts5():
    """获取 FTS5 实例。"""
    try:
        from wrapper.fts5_store import get_fts5
        return get_fts5()
    except Exception:
        return None


def _get_salience():
    """获取 salience 模块。"""
    try:
        from wrapper import salience
        return salience
    except Exception:
        return None


def _get_version_tracker():
    """获取 version_tracker 模块。"""
    try:
        from wrapper import version_tracker
        return version_tracker
    except Exception:
        return None


def sync_after_delete(memory_id: str, user_id: str = "bo") -> None:
    """删除记忆后同步清理 FTS5/salience/version_tracker。

    调用方：evolve_mem、auto_expire
    任一子系统失败则入补偿队列（action="delete"）。
    """
    failures = []

    # FTS5
    fts5 = _get_fts5()
    if fts5:
        try:
            fts5.delete(memory_id)
            logger.debug("IndexSync: FTS5 删除 %s", memory_id[:8])
        except Exception as e:
            logger.warning("IndexSync: FTS5 删除失败 %s: %s", memory_id[:8], e)
            failures.append(("fts5", str(e)))

    # salience
    sal = _get_salience()
    if sal:
        try:
            sal.delete(memory_id)
            logger.debug("IndexSync: salience 删除 %s", memory_id[:8])
        except Exception as e:
            logger.warning("IndexSync: salience 删除失败 %s: %s", memory_id[:8], e)
            failures.append(("salience", str(e)))

    # version_tracker
    vt = _get_version_tracker()
    if vt:
        try:
            vt.cleanup(memory_id)
            logger.debug("IndexSync: version_tracker 清理 %s", memory_id[:8])
        except Exception as e:
            logger.warning("IndexSync: version_tracker 清理失败 %s: %s", memory_id[:8], e)
            failures.append(("version_tracker", str(e)))

    # 入补偿队列
    if failures:
        try:
            from security.compensation import enqueue
            enqueue(
                content=memory_id,
                filters={"user_id": user_id},
                metadata={"failures": failures},
                action="delete",
            )
            logger.info("IndexSync: delete %s 补偿入队 (%d个子系统失败)", memory_id[:8], len(failures))
        except Exception as e:
            logger.warning("IndexSync: delete 补偿入队失败: %s", e)


def sync_after_merge(new_id: str, old_ids: list, merged_text: str,
                     user_id: str = "bo", merged_metadata: dict = None) -> None:
    """合并记忆后同步更新 FTS5/salience。

    调用方：consolidation
    """
    # FTS5：删除旧索引 + 写入新索引
    fts5 = _get_fts5()
    if fts5:
        try:
            for old_id in old_ids:
                fts5.delete(old_id)
            fts5.write(new_id, merged_text, user_id)
            logger.debug("IndexSync: FTS5 合并 %d→1", len(old_ids))
        except Exception as e:
            logger.warning("IndexSync: FTS5 合并失败: %s", e)

    # salience：注册新记忆
    sal = _get_salience()
    if sal:
        try:
            sal.register(new_id, content_preview=merged_text[:200])
            for old_id in old_ids:
                try:
                    sal.delete(old_id)
                except Exception:
                    pass
            logger.debug("IndexSync: salience 合并 %d→1", len(old_ids))
        except Exception as e:
            logger.warning("IndexSync: salience 合并失败: %s", e)

    # version_tracker：已由 memory.update() 归档处理，无需额外操作


async def compensate_delete(memory_id: str, filters: dict, metadata: dict = None) -> dict:
    """补偿队列 delete handler：重新执行 sync_after_delete。

    供 compensation worker 调用，签名与 safe_add 一致。
    content=memory_id, filters={"user_id": ...}
    """
    user_id = (filters or {}).get("user_id", "bo")
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, sync_after_delete, memory_id, user_id)
        return {"action": "ok", "memory_id": memory_id}
    except Exception as e:
        logger.warning("compensate_delete 失败 %s: %s", memory_id[:8], e)
        return {"action": "error", "memory_id": memory_id, "error": str(e)}
