#!/usr/bin/env python3
"""backfill_fts5.py — 从 Qdrant 回填 FTS5 全文索引。

用法:
  # dry-run（只统计，不写入）
  python3 scripts/backfill_fts5.py --dry-run

  # 执行回填
  python3 scripts/backfill_fts5.py

  # 指定 batch 大小
  python3 scripts/backfill_fts5.py --batch-size 50

  # 指定 FTS5 数据库路径
  python3 scripts/backfill_fts5.py --fts5-db /path/to/fts5.db
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error

logger = logging.getLogger("mem0x.backfill")

# ── 配置 ──
QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:26333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "mem0")
BATCH_SIZE = 200


def qdrant_scroll(offset=None, limit=BATCH_SIZE):
    """Scroll Qdrant 获取一批 points。"""
    body = {
        "limit": limit,
        "with_payload": True,
        "with_vectors": False,
    }
    if offset is not None:
        body["offset"] = offset

    url = f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if QDRANT_API_KEY:
        req.add_header("api-key", QDRANT_API_KEY)

    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser(description="从 Qdrant 回填 FTS5 全文索引")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="每批处理数量")
    parser.add_argument("--fts5-db", type=str, default=None, help="FTS5 数据库路径")
    args = parser.parse_args()

    # 初始化 FTS5
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from wrapper.fts5_store import FTS5Store

    fts5_config = None
    if args.fts5_db:
        fts5_config = {"db_path": args.fts5_db}
    fts5 = FTS5Store(fts5_config)

    # 获取已有 FTS5 ID 集合
    existing_ids = set()
    try:
        rows = fts5._conn.execute("SELECT memory_id FROM fts5_meta").fetchall()
        existing_ids = {row[0] for row in rows}
    except Exception as e:
        logger.debug("fts5_meta query", exc_info=True)
    print(f"FTS5 已有记录: {len(existing_ids)}")

    # Scroll Qdrant 获取所有活跃记忆
    total = 0
    already_exists = 0
    new_written = 0
    failed = 0
    skipped_deleted = 0
    offset = None
    pending = []  # 待写入缓冲

    print(f"开始从 Qdrant scroll (batch={args.batch_size})...")
    t0 = time.time()

    while True:
        try:
            result = qdrant_scroll(offset=offset, limit=args.batch_size)
        except Exception as e:
            logger.warning("Qdrant scroll: %s", e)
            print(f"Qdrant scroll 失败: {e}")
            break

        points = result.get("result", {}).get("points", [])
        next_offset = result.get("result", {}).get("next_page_offset")

        if not points:
            break

        for pt in points:
            total += 1
            memory_id = pt.get("id", "")
            payload = pt.get("payload") or {}

            # 跳过已删除的记忆
            if payload.get("deleted_at"):
                skipped_deleted += 1
                continue

            # 检查 FTS5 是否已有
            if memory_id in existing_ids:
                already_exists += 1
                continue

            # 提取内容和 user_id
            content = payload.get("data", "") or payload.get("memory", "")
            user_id = payload.get("user_id", "")

            if not content:
                continue

            if args.dry_run:
                new_written += 1
                continue

            # 写入 FTS5（每 100 条 commit 一次）
            pending.append((memory_id, content, user_id))
            if len(pending) >= 100:
                for mid, text, uid in pending:
                    try:
                        fts5.write(mid, text, uid)
                    except Exception as e:
                        logger.warning("FTS5 write %s: %s", mid[:12], e)
                        failed += 1
                        print(f"  写入失败 {mid[:12]}: {e}")
                fts5._conn.commit()
                new_written += len(pending) - failed
                pending = []
                print(f"  已处理 {total} 条 (新写入 {new_written})...")

        if next_offset is None or next_offset == offset:
            break
        offset = next_offset

    # 处理剩余
    if pending and not args.dry_run:
        for mid, text, uid in pending:
            try:
                fts5.write(mid, text, uid)
            except Exception as e:
                logger.warning("FTS5 write remaining %s: %s", mid[:12], e)
                failed += 1
                print(f"  写入失败 {mid[:12]}: {e}")
        fts5._conn.commit()
        new_written += len(pending) - failed

    elapsed = time.time() - t0

    # 输出统计
    print("\n" + "=" * 50)
    print(f"回填{'（dry-run）' if args.dry_run else ''} 完成:")
    print(f"  Qdrant 总数:     {total}")
    print(f"  已删除跳过:      {skipped_deleted}")
    print(f"  FTS5 已存在:     {already_exists}")
    print(f"  新写入:          {new_written}")
    print(f"  失败:            {failed}")
    print(f"  耗时:            {elapsed:.1f}s")
    print("=" * 50)

    if args.dry_run:
        print("\n提示: 去掉 --dry-run 执行实际回填")


if __name__ == "__main__":
    main()
