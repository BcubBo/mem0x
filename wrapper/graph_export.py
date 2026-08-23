"""graph_export — 知识图谱可视化导出

从 Neo4j 导出节点+边数据，供 Hermes 调用或前端消费。
纯 Cypher 查询，无 LLM 调用，快且便宜。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("mem0x.graph_export")


ALLOWED_ENTITY_TYPES = frozenset({
    "Entity", "Person", "Project", "Service", "Config",
    "Module", "Tool", "Concept",
})


def export_graph(
    limit: int = 200,
    depth: int = 2,
    entity_type: Optional[str] = None,
    center: Optional[str] = None,
) -> Dict[str, Any]:
    """导出 Neo4j 图数据。

    Args:
        limit: 最大节点数
        depth: 从中心节点展开的层数（仅 center 模式生效）
        entity_type: 按节点类型过滤（Person/Project/Service/...）
        center: 中心节点名称（指定时做子图导出，不指定时全局导出）

    Returns:
        {nodes: [...], edges: [...], stats: {...}}
    """
    from wrapper.neo4j_hook import get_hook

    # 白名单校验：防止 Cypher 注入
    if entity_type and entity_type not in ALLOWED_ENTITY_TYPES:
        return {"nodes": [], "edges": [], "stats": {"total_nodes": 0, "total_edges": 0},
                "error": f"invalid entity_type: {entity_type}. allowed: {sorted(ALLOWED_ENTITY_TYPES)}"}

    # 边界检查：防止 depth 过大导致 Neo4j 卡死
    depth = max(1, min(depth, 5))
    limit = max(1, min(limit, 1000))

    hook = get_hook()
    if not hook.enabled or not hook._driver:
        return {"nodes": [], "edges": [], "stats": {"total_nodes": 0, "total_edges": 0}, "error": "neo4j not connected"}

    try:
        with hook._driver.session() as session:
            if center:
                return _export_subgraph(session, center, limit, depth, entity_type)
            else:
                return _export_full(session, limit, entity_type)
    except Exception as e:
        logger.warning("graph_export 失败: %s", e)
        return {"nodes": [], "edges": [], "stats": {"total_nodes": 0, "total_edges": 0}, "error": str(e)}


def _export_full(session, limit: int, entity_type: Optional[str]) -> Dict[str, Any]:
    """全局导出：按连接数排序取 top-N 节点及其边。"""
    # 构建类型过滤
    type_filter = ""
    if entity_type:
        type_filter = f"WHERE '{entity_type}' IN labels(n)"

    # 取连接数最多的节点
    query_nodes = f"""
        MATCH (n)
        {type_filter}
        WITH n, size([(n)--() | 1]) AS connections
        ORDER BY connections DESC
        LIMIT $limit
        RETURN n.name AS name, n.original_name AS original_name,
               labels(n) AS labels, connections
    """
    node_records = session.run(query_nodes, limit=limit).data()

    # 收集节点名集合
    node_names = set()
    nodes = []
    for r in node_records:
        name = r["original_name"] or r["name"]
        node_names.add(r["name"])
        nodes.append({
            "id": name,
            "type": r["labels"][0] if r["labels"] else "Entity",
            "connections": r["connections"],
        })

    if not node_names:
        return {"nodes": [], "edges": [], "stats": {"total_nodes": 0, "total_edges": 0}}

    # 取这些节点之间的边
    query_edges = """
        MATCH (a)-[r]->(b)
        WHERE a.name IN $names AND b.name IN $names
        RETURN a.name AS source, b.name AS target, type(r) AS rel_type
    """
    edge_records = session.run(query_edges, names=list(node_names)).data()

    # 去重边
    seen_edges = set()
    edges = []
    for r in edge_records:
        key = (r["source"], r["target"], r["rel_type"])
        if key not in seen_edges:
            seen_edges.add(key)
            edges.append({
                "source": r["source"],
                "target": r["target"],
                "type": r["rel_type"],
            })

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
        },
    }


def _export_subgraph(
    session, center: str, limit: int, depth: int, entity_type: Optional[str]
) -> Dict[str, Any]:
    """子图导出：以 center 为起点，展开 depth 层。"""
    # 按深度展开的 variable-length pattern
    type_filter = ""
    if entity_type:
        type_filter = f"WHERE '{entity_type}' IN labels(m)"

    query = f"""
        MATCH path = (center {{name: toLower($center)}})-[*1..{depth}]-(m)
        {type_filter}
        WITH DISTINCT m, size([(m)--() | 1]) AS connections
        LIMIT $limit
        RETURN m.name AS name, m.original_name AS original_name,
               labels(m) AS labels, connections
    """
    node_records = session.run(query, center=center, limit=limit).data()

    node_names = set()
    nodes = []

    # 先加中心节点
    center_rec = session.run(
        "MATCH (n {name: toLower($center)}) "
        "RETURN n.name AS name, n.original_name AS original_name, "
        "labels(n) AS labels, size([(n)--() | 1]) AS connections",
        center=center,
    ).data()
    if center_rec:
        r = center_rec[0]
        name = r["original_name"] or r["name"]
        node_names.add(r["name"])
        nodes.append({
            "id": name,
            "type": r["labels"][0] if r["labels"] else "Entity",
            "connections": r["connections"],
            "is_center": True,
        })

    for r in node_records:
        name = r["original_name"] or r["name"]
        if r["name"] not in node_names:
            node_names.add(r["name"])
            nodes.append({
                "id": name,
                "type": r["labels"][0] if r["labels"] else "Entity",
                "connections": r["connections"],
            })

    if not node_names:
        return {"nodes": [], "edges": [], "stats": {"total_nodes": 0, "total_edges": 0}}

    # 取这些节点之间的边
    query_edges = """
        MATCH (a)-[r]->(b)
        WHERE a.name IN $names AND b.name IN $names
        RETURN a.name AS source, b.name AS target, type(r) AS rel_type
    """
    edge_records = session.run(query_edges, names=list(node_names)).data()

    seen_edges = set()
    edges = []
    for r in edge_records:
        key = (r["source"], r["target"], r["rel_type"])
        if key not in seen_edges:
            seen_edges.add(key)
            edges.append({
                "source": r["source"],
                "target": r["target"],
                "type": r["rel_type"],
            })

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "center": center,
            "depth": depth,
        },
    }
