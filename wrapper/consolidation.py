"""consolidation v2 — 记忆碎片合并（增强版）

功能：
1. 按主题聚类：提取实体 + 向量相似度双重判定
2. LLM 合并：多条碎片 → 1条完整事实链（保留变更历史）
3. 归档旧碎片：标记 archived=true，不删除
4. SQLite 追踪合并历史

改动：wrapper/consolidation.py（替换旧版）
"""
from __future__ import annotations
import asyncio

import logging
import math
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("mem0x.consolidation")

# ── 配置 ──────────────────────────────────────────────
# 向量相似度阈值（余弦相似度，高于此值视为可合并）
VECTOR_SIM_THRESHOLD = 0.82

# Jaccard 文本相似度阈值（双重验证）
JACCARD_THRESHOLD = 0.45

# 最小/最大合并组大小
MIN_GROUP_SIZE = 2
MAX_GROUP_SIZE = 8

# 每轮最大合并数（防止一次处理太多）
MAX_MERGES_PER_CYCLE = 5

# 后台扫描间隔（秒）
DEFAULT_INTERVAL = 7200  # 2小时

# 记忆最小长度（太短的不合并）
MIN_MEMORY_LENGTH = 15

# ── 全局状态 ──────────────────────────────────────────
_running = False
_thread: Optional[threading.Thread] = None
_merge_lock = threading.Lock()


# ═══════════════════════════════════════════════════════
# SQLite 追踪
# ═══════════════════════════════════════════════════════

_db_path: Optional[str] = None
_schema_checked = False
_schema_lock = threading.Lock()


def _get_db_path() -> str:
    global _db_path
    if _db_path is None:
        from security.utils import get_data_dir
        _db_path = os.path.join(get_data_dir(), "consolidation.db")
    return _db_path


def _ensure_schema() -> None:
    global _schema_checked
    with _schema_lock:
        if _schema_checked:
            return
        db_path = _get_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        try:
            conn = sqlite3.connect(db_path, timeout=10)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS merge_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    merged_id   TEXT NOT NULL,
                    source_ids  TEXT NOT NULL,
                    merged_text TEXT NOT NULL,
                    created_at  REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS archived_memories (
                    memory_id   TEXT PRIMARY KEY,
                    merged_into TEXT NOT NULL,
                    archived_at REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mh_merged ON merge_history(merged_id)"
            )
            conn.commit()
            conn.close()
            _schema_checked = True
        except Exception as e:
            logger.warning("consolidation schema 初始化失败: %s", e)


def _record_merge(merged_id: str, source_ids: List[str], merged_text: str) -> None:
    """记录合并历史。"""
    _ensure_schema()
    import json
    conn = sqlite3.connect(_get_db_path(), timeout=10)
    try:
        conn.execute(
            "INSERT INTO merge_history (merged_id, source_ids, merged_text, created_at) VALUES (?,?,?,?)",
            (merged_id, json.dumps(source_ids), merged_text, time.time()),
        )
        conn.commit()
    except Exception as e:
        logger.debug("merge_history insert 失败: %s", e)
    finally:
        conn.close()
    # 同步更新缓存
    if _merge_cache is not None:
        _merge_cache.add(tuple(sorted(source_ids)))


def _record_archive(memory_id: str, merged_into: str) -> None:
    """记录归档。"""
    _ensure_schema()
    conn = sqlite3.connect(_get_db_path(), timeout=10)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO archived_memories (memory_id, merged_into, archived_at) VALUES (?,?,?)",
            (memory_id, merged_into, time.time()),
        )
        conn.commit()
    except Exception as e:
        logger.debug("archived_memories insert 失败: %s", e)
    finally:
        conn.close()


def _get_archived_ids() -> Set[str]:
    """获取已归档的记忆 ID 集合。"""
    _ensure_schema()
    conn = sqlite3.connect(_get_db_path(), timeout=10)
    try:
        rows = conn.execute("SELECT memory_id FROM archived_memories").fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()
    finally:
        conn.close()


_merge_cache: Optional[set] = None
_merge_cache_at: float = 0


def _is_already_merged(source_ids: List[str]) -> bool:
    """检查这组源记忆是否已经合并过（带内存缓存）。"""
    global _merge_cache, _merge_cache_at
    _ensure_schema()
    import json

    # 缓存5分钟
    now = time.time()
    if _merge_cache is None or now - _merge_cache_at > 300:
        conn = sqlite3.connect(_get_db_path(), timeout=10)
        try:
            rows = conn.execute("SELECT source_ids FROM merge_history").fetchall()
            _merge_cache = set()
            for r in rows:
                try:
                    ids_tuple = tuple(sorted(json.loads(r[0])))
                    _merge_cache.add(ids_tuple)
                except Exception:
                    pass
            _merge_cache_at = now
        except Exception:
            _merge_cache = set()
        finally:
            conn.close()

    key = tuple(sorted(source_ids))
    return key in _merge_cache


# ═══════════════════════════════════════════════════════
# 实体提取（简化版，用于聚类）
# ═══════════════════════════════════════════════════════

# 中文停用词
_ZH_STOP = frozenset({
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
    "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
    "你", "会", "着", "没有", "看", "好", "自己", "这", "那", "被",
    "把", "让", "给", "对", "从", "为", "以", "但", "而", "如果",
    "因为", "所以", "这个", "那个", "什么", "怎么", "可以", "已经",
    "之后", "之前", "通过", "使用", "需要", "进行", "实现", "完成",
    "应该", "能够", "可能", "目前", "当前", "现在", "这里", "那里",
})


def _extract_keywords(text: str) -> Set[str]:
    """从文本中提取关键词（用于实体聚类）。"""
    keywords = set()

    # 英文单词（大驼峰或全大写，如 Qdrant, Neo4j, docker）
    for m in re.finditer(r'\b[A-Za-z][A-Za-z0-9_-]{2,}\b', text):
        w = m.group()
        if w.lower() not in ('the', 'and', 'for', 'with', 'from', 'this', 'that', 'are', 'was'):
            keywords.add(w)

    # 中文词（2-6字，排除停用词）
    for m in re.finditer(r'[\u4e00-\u9fa5]{2,6}', text):
        w = m.group()
        if w not in _ZH_STOP:
            keywords.add(w)

    # 数字+单位（端口号、版本号等）
    for m in re.finditer(r'\b\d{2,5}\b', text):
        keywords.add(m.group())

    return keywords


# ═══════════════════════════════════════════════════════
# 相似度计算
# ═══════════════════════════════════════════════════════

def _jaccard(set_a: Set[str], set_b: Set[str]) -> float:
    """Jaccard 相似度。"""
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def _compute_embedding_similarity_matrix(
    texts: List[str], embedding_model
) -> Optional[Any]:
    """计算嵌入向量的余弦相似度矩阵。

    Returns:
        numpy ndarray (n x n) 或 None（如果 numpy 不可用）
    """
    try:
        import numpy as np
    except ImportError:
        logger.warning("numpy 不可用，跳过向量相似度计算")
        return None

    try:
        # 批量嵌入
        embeddings = embedding_model.embed_batch(texts)
        if not embeddings:
            return None

        # 转为 numpy 矩阵
        matrix = np.array(embeddings, dtype=np.float32)
        # 归一化
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        matrix = matrix / norms

        # 余弦相似度 = 点积（已归一化）
        sim_matrix = matrix @ matrix.T
        return sim_matrix

    except Exception as e:
        logger.error("嵌入相似度计算失败: %s", e)
        return None


# ═══════════════════════════════════════════════════════
# Union-Find（连通分量聚类）
# ═══════════════════════════════════════════════════════

class _UnionFind:
    """并查集，用于连通分量聚类。"""

    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1

    def clusters(self) -> Dict[int, List[int]]:
        """返回聚类结果 {root: [members]}。"""
        groups: Dict[int, List[int]] = {}
        for i in range(len(self.parent)):
            root = self.find(i)
            groups.setdefault(root, []).append(i)
        return groups


# ═══════════════════════════════════════════════════════
# LLM 合并
# ═══════════════════════════════════════════════════════

MERGE_PROMPT = """你是一个记忆整合专家。给定一组关于同一主题的碎片记忆，请将它们合并为一条完整、准确的事实链。

规则：
1. 保留所有关键事实（端口号、配置路径、版本号等具体值）
2. 如果有变更历史（如"端口从A改为B"），保留最终状态并简要提及变更
3. 去除重复信息
4. 输出一条简洁、完整的事实陈述（中文）
5. 不要添加碎片中没有的信息
6. 保持原有标签格式（如 [P0][tech] 等）

碎片记忆：
{fragments}

请输出合并后的完整事实（只输出事实本身，不要解释）："""


def _get_consolidation_llm(memory):
    """获取 consolidation 专用 LLM 客户端。

    优先读 config.json 的 consolidation.llm 配置（8B，便宜），
    降级到主 mem0 LLM（14B）。
    """
    try:
        from wrapper.mem0_runtime import load_config
        config = load_config()
        cons_cfg = config.get("consolidation", {}).get("llm", {})
        if cons_cfg.get("config", {}).get("model"):
            # 独立 LLM 配置
            import openai
            llm_cfg = cons_cfg["config"]
            client = openai.OpenAI(
                api_key=llm_cfg["api_key"],
                base_url=llm_cfg.get("openai_base_url", "https://api.siliconflow.cn/v1"),
            )
            model = llm_cfg["model"]

            class _ConsolidationLLM:
                def generate_response(self, messages, **kwargs):
                    resp = client.chat.completions.create(
                        model=model, messages=messages, max_tokens=512,
                    )
                    return resp.choices[0].message.content

            logger.info("consolidation 使用独立 LLM: %s", model)
            return _ConsolidationLLM()
    except Exception as e:
        logger.debug("consolidation 独立 LLM 加载失败，降级到主 LLM: %s", e)

    # 降级：用主 mem0 LLM
    logger.info("consolidation 降级使用主 LLM")
    return memory.llm


def _merge_with_llm(
    fragments: List[str], llm_client, max_retries: int = 2
) -> Optional[str]:
    """用 LLM 合并多条碎片记忆。"""
    # 格式化碎片
    frag_text = "\n".join(f"  [{i+1}] {f}" for i, f in enumerate(fragments))
    prompt = MERGE_PROMPT.format(fragments=frag_text)

    messages = [{"role": "user", "content": prompt}]

    for attempt in range(max_retries + 1):
        try:
            response = llm_client.generate_response(messages)
            if response and isinstance(response, str):
                # 清理输出
                merged = response.strip()
                # 去掉可能的引号包裹
                if (merged.startswith('"') and merged.endswith('"')) or \
                   (merged.startswith("'") and merged.endswith("'")):
                    merged = merged[1:-1]
                # 去掉 "合并结果：" 之类的前缀
                for prefix in ["合并结果：", "合并结果:", "合并后：", "合并后:", "结果：", "结果:"]:
                    if merged.startswith(prefix):
                        merged = merged[len(prefix):].strip()
                if merged and len(merged) >= MIN_MEMORY_LENGTH:
                    return merged
                logger.debug("LLM 输出太短或为空: %r", merged)
            else:
                logger.debug("LLM 返回异常: %s", type(response))
        except Exception as e:
            logger.warning("LLM 合并尝试 %d 失败: %s", attempt + 1, e)
            if attempt < max_retries:
                time.sleep(2)

    return None


# ═══════════════════════════════════════════════════════
# 核心合并流程
# ═══════════════════════════════════════════════════════

async def find_merge_groups(
    memory,
    user_id: str = "bo",
    agent_id: str = "hermes",
    top_k: int = 500,
) -> List[List[Dict]]:
    """查找可合并的记忆组。

    流程：
    1. get_all 拉全量记忆
    2. 过滤已归档的
    3. 提取关键词 + 计算向量相似度
    4. Union-Find 聚类
    5. 返回可合并组（size >= MIN_GROUP_SIZE）

    Returns:
        List[List[Dict]] — 每个子列表是一组可合并的记忆
    """
    # 1. 拉全量
    filters = {"user_id": user_id}
    if agent_id:
        filters["agent_id"] = agent_id

    try:
        result = await memory.get_all(filters=filters, top_k=top_k)
        items = result.get("results", []) if isinstance(result, dict) else []
    except Exception as e:
        logger.error("get_all 失败: %s", e)
        return []

    if len(items) < MIN_GROUP_SIZE:
        return []

    # 2. 过滤已归档 + 太短的
    archived_ids = _get_archived_ids()
    candidates = []
    for item in items:
        mid = item.get("id", "")
        text = item.get("memory", "")
        if not mid or not text:
            continue
        if mid in archived_ids:
            continue
        if len(text) < MIN_MEMORY_LENGTH:
            continue
        # 跳过 metadata.archived=true 的（矛盾消解归档的）
        meta = item.get("metadata") or {}
        if meta.get("archived") or meta.get("deleted_at"):
            continue
        candidates.append(item)

    if len(candidates) < MIN_GROUP_SIZE:
        return []

    logger.info("consolidation: %d 候选记忆（总 %d，归档跳过 %d）",
                len(candidates), len(items), len(items) - len(candidates))

    # 3. 提取关键词
    keywords_list = [_extract_keywords(item.get("memory", "")) for item in candidates]

    # 4. 计算向量相似度（批量嵌入）
    texts = [item.get("memory", "") for item in candidates]
    sim_matrix = None
    try:
        sim_matrix = _compute_embedding_similarity_matrix(texts, memory.embedding_model)
    except Exception as e:
        logger.warning("向量相似度计算失败，降级为纯关键词: %s", e)

    # 5. Union-Find 聚类
    n = len(candidates)
    uf = _UnionFind(n)

    for i in range(n):
        for j in range(i + 1, n):
            # 双重验证：关键词重叠 + 向量相似度
            kw_sim = _jaccard(keywords_list[i], keywords_list[j])
            vec_sim = float(sim_matrix[i][j]) if sim_matrix is not None else 0.0

            # 关键词相似度必须达标
            if kw_sim < JACCARD_THRESHOLD:
                continue

            # 向量相似度也要达标（如果有的话）
            if sim_matrix is not None and vec_sim < VECTOR_SIM_THRESHOLD:
                continue

            # 两个条件都满足，合并
            uf.union(i, j)

    # 6. 提取聚类结果
    clusters = uf.clusters()
    groups = []
    for root, members in clusters.items():
        if len(members) >= MIN_GROUP_SIZE and len(members) <= MAX_GROUP_SIZE:
            group = [candidates[i] for i in members]
            groups.append(group)

    # 按组大小降序（先处理大组）
    groups.sort(key=lambda g: len(g), reverse=True)

    logger.info("consolidation: 发现 %d 个可合并组", len(groups))
    return groups


async def run_consolidation_cycle(
    memory,
    neo4j_hook=None,
    user_id: str = "bo",
    agent_id: str = "hermes",
) -> int:
    """执行一轮记忆整合，返回合并数量。

    流程：
    1. find_merge_groups — 聚类
    2. 对每组调用 LLM 合并
    3. 写入合并结果，归档旧碎片
    4. 清理 Neo4j
    """
    if not _merge_lock.acquire(blocking=False):
        logger.info("consolidation: 另一轮正在执行，跳过")
        return 0

    merged_count = 0
    try:
        groups = await find_merge_groups(memory, user_id, agent_id)

        for group in groups[:MAX_MERGES_PER_CYCLE]:
            source_ids = [item.get("id", "") for item in group]
            source_texts = [item.get("memory", "") for item in group]

            # 检查是否已合并过
            if _is_already_merged(source_ids):
                logger.debug("跳过已合并组: %s", source_ids[0][:8])
                continue

            logger.info("合并 %d 条碎片: %s ...",
                        len(group), source_texts[0][:50])

            # LLM 合并（使用 consolidation 专用 LLM）
            llm = _get_consolidation_llm(memory)
            merged_text = _merge_with_llm(source_texts, llm)
            if not merged_text:
                logger.warning("LLM 合并失败，跳过这组")
                continue

            # 写入合并结果
            # 注意：infer=False 时 messages 必须是 [{"role":"user","content":"..."}] 格式
            try:
                new_result = await memory.add(
                    messages=[{"role": "user", "content": merged_text}],
                    user_id=user_id,
                    agent_id=agent_id,
                    metadata={
                        "source": "consolidation",
                        "merged_from": source_ids,
                        "merge_count": len(source_ids),
                    },
                    infer=False,
                )
                # mem0 返回 {"results": [{"id": "...", ...}]}
                results_list = new_result.get("results", []) if isinstance(new_result, dict) else []
                new_id = results_list[0].get("id") if results_list else None
                if not new_id:
                    logger.warning("写入合并结果失败")
                    continue
            except Exception as e:
                logger.error("写入合并结果失败: %s", e)
                continue

            # 归档旧碎片（标记 archived=true）
            for item in group:
                old_id = item.get("id", "")
                if not old_id:
                    continue
                try:
                    await memory.update(
                        old_id,
                        metadata={"archived": True, "merged_into": new_id},
                    )
                    _record_archive(old_id, new_id)
                except Exception as e:
                    logger.debug("归档失败 %s: %s", old_id[:8], e)

                # 清理 Neo4j
                if neo4j_hook and neo4j_hook.enabled:
                    try:
                        neo4j_hook.cleanup(old_id)
                    except Exception as e:
                        logger.debug("Neo4j cleanup 失败 %s: %s", old_id[:8], e)

            # 记录合并历史
            _record_merge(new_id, source_ids, merged_text)
            merged_count += 1
            logger.info("✓ 合并完成: %d 条 → %s", len(source_ids), new_id[:8])

    except Exception as e:
        logger.error("consolidation 循环异常: %s", e)
    finally:
        _merge_lock.release()

    return merged_count


# ═══════════════════════════════════════════════════════
# 后台线程
# ═══════════════════════════════════════════════════════

def _background_loop(memory_getter, interval: int = DEFAULT_INTERVAL):
    """后台循环线程。"""
    global _running
    logger.info("consolidation v2 后台线程启动，间隔 %ds", interval)

    from wrapper.neo4j_hook import get_hook
    neo4j_hook = None
    try:
        neo4j_hook = get_hook()
    except Exception:
        pass

    while _running:
        try:
            memory = memory_getter()
            if memory:
                merged = asyncio.run(run_consolidation_cycle(memory, neo4j_hook=neo4j_hook))
                if merged > 0:
                    logger.info("本轮整合 %d 条记忆", merged)
        except Exception as e:
            logger.error("consolidation 循环异常: %s", e)

        time.sleep(interval)

    logger.info("consolidation v2 后台线程已停止")


def start(memory_getter, interval: int = DEFAULT_INTERVAL):
    """启动后台整合线程。"""
    global _running, _thread
    if _running:
        logger.warning("consolidation 已在运行")
        return

    _running = True
    _thread = threading.Thread(
        target=_background_loop,
        args=(memory_getter, interval),
        daemon=True,
        name="consolidation-v2",
    )
    _thread.start()


def stop():
    """停止后台整合线程。"""
    global _running
    _running = False


def is_running() -> bool:
    return _running
