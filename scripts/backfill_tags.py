#!/usr/bin/env python3
"""backfill_tags.py — 给 Qdrant 中没有 tags 的记忆补写 spaCy NER 实体标签。

用法:
  # dry-run（只统计，不写入）
  python3 backfill_tags.py --dry-run

  # 执行 backfill
  python3 backfill_tags.py

  # 指定 batch 大小
  python3 backfill_tags.py --batch-size 50
"""

import argparse
import json
import logging
import sys
import time
import urllib.request
import urllib.error

logger = logging.getLogger("mem0x.backfill")

# ── 配置 ──
QDRANT_URL = "http://127.0.0.1:26333"
QDRANT_API_KEY = "b16f7b9fbc154285906a69e9438fcafdb5c70b397fa07678169a0b127d2cecb4"
COLLECTION = "mem0"


def qdrant_request(method: str, path: str, body: dict = None) -> dict:
    """发送 Qdrant REST API 请求。"""
    url = f"{QDRANT_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("api-key", QDRANT_API_KEY)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  ❌ HTTP {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        return {}


def extract_tags_spacy(content: str, top_n: int = 10) -> list[str]:
    """用 spaCy NER 提取实体标签。"""
    try:
        import spacy
        nlp = spacy.load("zh_core_web_sm")
    except Exception as e:
        logger.warning("spaCy zh model load: %s", e)
        try:
            import spacy
            nlp = spacy.load("en_core_web_sm")
        except Exception as e:
            logger.warning("spaCy en model load: %s", e)
            print(f"  ⚠️ spaCy 模型加载失败: {e}", file=sys.stderr)
            return []

    STOP = frozenset({
        "the", "a", "an", "is", "are", "was", "的", "了", "在", "是",
        "和", "有", "为", "这", "中", "不", "也", "用户", "助手",
        "memory", "data", "config", "system", "test", "tool",
    })

    try:
        doc = nlp(content[:10000])
        from collections import Counter
        entities = [
            ent.text.strip() for ent in doc.ents
            if len(ent.text.strip()) >= 2 and ent.text.strip().lower() not in STOP
        ]
        counter = Counter(entities)
        return [word for word, _ in counter.most_common(top_n)]
    except Exception as e:
        logger.warning("NER extraction: %s", e)
        print(f"  ⚠️ NER 失败: {e}", file=sys.stderr)
        return []


def scroll_all_points(batch_size: int = 100):
    """滚动遍历所有点，返回 (points_without_tags, total_count)。"""
    cursor = None
    total = 0
    without_tags = []

    while True:
        body = {
            "limit": batch_size,
            "with_payload": {"include": ["tags", "data", "text_lemmatized"]},
            "with_vectors": False,
        }
        if cursor:
            body["offset"] = cursor

        resp = qdrant_request("POST", f"/collections/{COLLECTION}/points/scroll", body)
        result = resp.get("result", {})
        points = result.get("points", [])
        next_cursor = result.get("next_page_offset")

        for p in points:
            total += 1
            payload = p.get("payload", {})
            if not payload.get("tags"):
                # 取文本内容
                content = payload.get("data", "") or payload.get("text_lemmatized", "") or ""
                without_tags.append({"id": p["id"], "content": content})

        if not next_cursor or not points:
            break
        cursor = next_cursor

    return without_tags, total


def batch_set_payload(updates: list[dict]):
    """批量更新 Qdrant payload。"""
    if not updates:
        return
    body = {
        "payload": {"tags": None},  # placeholder, will override per-point
    }
    # Qdrant 的 batch update 需要用 points 逐个 set
    # 但 set_payload 支持批量 points
    # 这里用 set_payload API
    pass  # 用下面的逐条方式


def set_tags_batch(points_with_tags: list[dict]):
    """批量设置 tags（用 Qdrant set_payload API）。"""
    if not points_with_tags:
        return 0

    # Qdrant set_payload 支持一次设多个 point 的同一字段
    # 但每个 point 的 tags 值不同，需要逐个设
    # 用 batch API: POST /collections/{name}/points/payload
    success = 0
    for item in points_with_tags:
        body = {
            "payload": {"tags": item["tags"]},
            "points": [item["id"]],
        }
        resp = qdrant_request("POST", f"/collections/{COLLECTION}/points/payload", body)
        if resp.get("status") == "ok" or resp.get("result"):
            success += 1
        else:
            print(f"  ❌ 写入失败 id={item['id'][:12]}: {resp}", file=sys.stderr)

    return success


def main():
    parser = argparse.ArgumentParser(description="Backfill tags for memories without tags")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--batch-size", type=int, default=100, help="滚动批次大小")
    args = parser.parse_args()

    print("📦 Qdrant backfill tags")
    print(f"   URL: {QDRANT_URL}")
    print(f"   Collection: {COLLECTION}")
    print()

    # Step 1: 统计
    print("🔍 Step 1: 扫描所有记忆...")
    start = time.time()
    without_tags, total = scroll_all_points(args.batch_size)
    elapsed = time.time() - start
    print(f"   总计: {total} 条记忆")
    print(f"   缺 tags: {len(without_tags)} 条")
    print(f"   已有 tags: {total - len(without_tags)} 条")
    print(f"   扫描耗时: {elapsed:.1f}s")
    print()

    if not without_tags:
        print("✅ 所有记忆都有 tags，无需 backfill")
        return

    if args.dry_run:
        print("🔒 dry-run 模式，不执行写入")
        # 看几条样例
        print("\n📋 样例（前5条）:")
        for item in without_tags[:5]:
            content = item["content"][:80]
            print(f"   id={item['id'][:16]}... content={content}")
        return

    # Step 2: NER + 写入
    print("🧠 Step 2: spaCy NER 提取 + 写入...")
    try:
        import spacy
        try:
            nlp = spacy.load("zh_core_web_sm")
            print("   模型: zh_core_web_sm")
        except Exception as e:
            logger.warning("spaCy zh model load in main: %s", e)
            nlp = spacy.load("en_core_web_sm")
            print("   模型: en_core_web_sm (降级)")
    except Exception as e:
        logger.warning("spaCy unavailable: %s", e)
        print(f"   ❌ spaCy 不可用: {e}")
        sys.exit(1)

    STOP = frozenset({
        "the", "a", "an", "is", "are", "was", "的", "了", "在", "是",
        "和", "有", "为", "这", "中", "不", "也", "用户", "助手",
        "memory", "data", "config", "system", "test", "tool",
    })

    updates = []
    ner_count = 0
    skip_count = 0

    for i, item in enumerate(without_tags):
        content = item["content"]
        if not content or len(content) < 10:
            skip_count += 1
            continue

        try:
            doc = nlp(content[:10000])
            from collections import Counter
            entities = [
                ent.text.strip() for ent in doc.ents
                if len(ent.text.strip()) >= 2 and ent.text.strip().lower() not in STOP
            ]
            tags = [w for w, _ in Counter(entities).most_common(10)]
            if tags:
                updates.append({"id": item["id"], "tags": tags})
                ner_count += 1
        except Exception as e:
            logger.warning("NER processing for id=%s: %s", item["id"][:12], e)
            skip_count += 1

        # 每 500 条打印进度
        if (i + 1) % 500 == 0:
            print(f"   进度: {i+1}/{len(without_tags)} (提取到 {ner_count} 条 tags)")

    print(f"   NER 完成: {ner_count} 条有 tags, {skip_count} 条跳过")
    print()

    # Step 3: 写入
    print(f"💾 Step 3: 写入 Qdrant ({len(updates)} 条)...")
    start = time.time()
    success = set_tags_batch(updates)
    elapsed = time.time() - start
    print(f"   成功: {success}/{len(updates)}")
    print(f"   耗时: {elapsed:.1f}s")
    print()

    # 汇总
    print("📊 汇总:")
    print(f"   总记忆: {total}")
    print(f"   本次 backfill: {success}")
    print(f"   已有 tags: {total - len(without_tags)}")
    print(f"   跳过(空/NER失败): {skip_count}")
    print("✅ done")


if __name__ == "__main__":
    main()
