"""working_memory 单元测试 — SQLite 读写、TTL 清理、搜索注入。

所有测试使用内存 SQLite (:memory:)，不碰真实数据库。
"""
import os
import sqlite3
import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 全局 patch db_common，让 working_memory 使用内存 SQLite
# 必须在 import working_memory 之前生效


@pytest.fixture(autouse=True)
def _in_memory_db(monkeypatch):
    """让 working_memory 使用内存 SQLite。"""
    # 每次测试重置 schema checked flag
    import wrapper.working_memory as wm
    wm._schema_checked["working_memory"] = False

    # 用一个共享的内存连接（带 check_same_thread=False 以便多连接兼容）
    # 注意：working_memory 每次调用 _get_db() 会创建新连接，
    # 但内存数据库 :memory: 每个连接是独立的，所以用文件 URI 方案
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = tmp.name
    tmp.close()

    def fake_get_db_path(name):
        return tmp_path

    def fake_ensure_schema(name, schema_sql, checked_flag):
        if checked_flag.get(name):
            return
        conn = sqlite3.connect(tmp_path, timeout=5)
        for sql in schema_sql:
            conn.execute(sql)
        conn.commit()
        conn.close()
        checked_flag[name] = True

    monkeypatch.setattr("security.db_common.get_db_path", fake_get_db_path)
    monkeypatch.setattr("security.db_common.ensure_schema", fake_ensure_schema)

    # 默认 config: enabled
    monkeypatch.setattr(
        "wrapper.working_memory._get_config",
        lambda: {"enabled": True, "default_ttl_days": 90, "injection_weight": 1.5},
    )

    yield tmp_path

    # 清理
    try:
        os.unlink(tmp_path)
    except OSError:
        pass


def _make_conn(path):
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


# ──────────────────────────────────────────────
# add / list_items
# ──────────────────────────────────────────────

class TestAddAndList:
    def test_add_returns_true(self, _in_memory_db):
        from wrapper.working_memory import add
        assert add("m1", "hello world", "user1") is True

    def test_add_then_list(self, _in_memory_db):
        from wrapper.working_memory import add, list_items
        add("m1", "content A", "user1")
        add("m2", "content B", "user1")
        items = list_items("user1")
        assert len(items) == 2
        contents = {it["content"] for it in items}
        assert contents == {"content A", "content B"}

    def test_list_isolation_between_users(self, _in_memory_db):
        from wrapper.working_memory import add, list_items
        add("m1", "u1 data", "user1")
        add("m2", "u2 data", "user2")
        assert len(list_items("user1")) == 1
        assert len(list_items("user2")) == 1

    def test_add_disabled(self, monkeypatch, _in_memory_db):
        from wrapper.working_memory import add
        monkeypatch.setattr(
            "wrapper.working_memory._get_config",
            lambda: {"enabled": False},
        )
        assert add("m1", "content", "user1") is False

    def test_add_custom_ttl(self, _in_memory_db):
        from wrapper.working_memory import add, list_items
        add("m1", "content", "user1", ttl_days=30)
        items = list_items("user1")
        assert items[0]["ttl_days"] == 30

    def test_add_default_ttl(self, _in_memory_db):
        from wrapper.working_memory import add, list_items
        add("m1", "content", "user1")
        items = list_items("user1")
        assert items[0]["ttl_days"] == 90

    def test_add_replaces_on_duplicate_memory_id(self, _in_memory_db):
        from wrapper.working_memory import add, list_items
        add("m1", "old content", "user1")
        add("m1", "new content", "user1")
        items = list_items("user1")
        assert len(items) == 1
        assert items[0]["content"] == "new content"


# ──────────────────────────────────────────────
# touch
# ──────────────────────────────────────────────

class TestTouch:
    def test_touch_increments_access_count(self, _in_memory_db):
        from wrapper.working_memory import add, touch, list_items
        add("m1", "content", "user1")
        touch("m1")
        touch("m1")
        items = list_items("user1")
        assert items[0]["access_count"] == 2

    def test_touch_updates_accessed_at(self, _in_memory_db):
        from wrapper.working_memory import add, touch, list_items
        add("m1", "content", "user1")
        before = list_items("user1")[0]["accessed_at"]
        time.sleep(0.05)
        touch("m1")
        after = list_items("user1")[0]["accessed_at"]
        assert after > before

    def test_touch_resets_ttl(self, _in_memory_db):
        from wrapper.working_memory import add, touch, list_items
        add("m1", "content", "user1", ttl_days=30)
        touch("m1", ttl_days=120)
        items = list_items("user1")
        assert items[0]["ttl_days"] == 120

    def test_touch_nonexistent_is_noop(self, _in_memory_db):
        from wrapper.working_memory import touch
        # should not raise
        touch("nonexistent_id")


# ──────────────────────────────────────────────
# delete_by_memory_id
# ──────────────────────────────────────────────

class TestDelete:
    def test_delete_existing(self, _in_memory_db):
        from wrapper.working_memory import add, delete_by_memory_id, list_items
        add("m1", "content", "user1")
        delete_by_memory_id("m1")
        assert len(list_items("user1")) == 0

    def test_delete_nonexistent_is_noop(self, _in_memory_db):
        from wrapper.working_memory import delete_by_memory_id
        delete_by_memory_id("nonexistent")

    def test_delete_only_target(self, _in_memory_db):
        from wrapper.working_memory import add, delete_by_memory_id, list_items
        add("m1", "A", "user1")
        add("m2", "B", "user1")
        delete_by_memory_id("m1")
        assert len(list_items("user1")) == 1
        assert list_items("user1")[0]["memory_id"] == "m2"


# ──────────────────────────────────────────────
# clear
# ──────────────────────────────────────────────

class TestClear:
    def test_clear_specific_user(self, _in_memory_db):
        from wrapper.working_memory import add, clear, list_items
        add("m1", "A", "user1")
        add("m2", "B", "user2")
        deleted = clear("user1")
        assert deleted == 1
        assert len(list_items("user1")) == 0
        assert len(list_items("user2")) == 1

    def test_clear_all(self, _in_memory_db):
        from wrapper.working_memory import add, clear, list_items
        add("m1", "A", "user1")
        add("m2", "B", "user2")
        deleted = clear()
        assert deleted == 2
        assert len(list_items("user1")) == 0
        assert len(list_items("user2")) == 0

    def test_clear_empty(self, _in_memory_db):
        from wrapper.working_memory import clear
        assert clear("nobody") == 0


# ──────────────────────────────────────────────
# gc_expired (TTL 清理)
# ──────────────────────────────────────────────

class TestGCExpired:
    def test_gc_removes_expired(self, _in_memory_db):
        from wrapper.working_memory import add, gc_expired, list_items
        # 插入一条已过期的记录（accessed_at 在过去，ttl=1 天）
        add("m1", "old content", "user1", ttl_days=1)
        # 手动将 accessed_at 改到 2 天前
        conn = _make_conn(_in_memory_db)
        two_days_ago = time.time() - 2 * 86400
        conn.execute(
            "UPDATE working_memory SET accessed_at=? WHERE memory_id=?",
            (two_days_ago, "m1"),
        )
        conn.commit()
        conn.close()

        deleted = gc_expired()
        assert deleted == 1
        assert len(list_items("user1")) == 0

    def test_gc_keeps_fresh(self, _in_memory_db):
        from wrapper.working_memory import add, gc_expired, list_items
        add("m1", "fresh", "user1", ttl_days=90)
        deleted = gc_expired()
        assert deleted == 0
        assert len(list_items("user1")) == 1

    def test_gc_mixed(self, _in_memory_db):
        from wrapper.working_memory import add, gc_expired, list_items
        add("m1", "expired", "user1", ttl_days=1)
        add("m2", "fresh", "user1", ttl_days=90)

        conn = _make_conn(_in_memory_db)
        two_days_ago = time.time() - 2 * 86400
        conn.execute(
            "UPDATE working_memory SET accessed_at=? WHERE memory_id=?",
            (two_days_ago, "m1"),
        )
        conn.commit()
        conn.close()

        deleted = gc_expired()
        assert deleted == 1
        remaining = list_items("user1")
        assert len(remaining) == 1
        assert remaining[0]["memory_id"] == "m2"


# ──────────────────────────────────────────────
# stats
# ──────────────────────────────────────────────

class TestStats:
    def test_stats_empty(self, _in_memory_db):
        from wrapper.working_memory import stats
        s = stats()
        assert s["total_items"] == 0
        assert s["expired_pending_gc"] == 0

    def test_stats_with_items(self, _in_memory_db):
        from wrapper.working_memory import add, stats
        add("m1", "A", "user1")
        add("m2", "B", "user1")
        s = stats()
        assert s["total_items"] == 2
        assert s["expired_pending_gc"] == 0

    def test_stats_counts_expired(self, _in_memory_db):
        from wrapper.working_memory import add, stats
        add("m1", "old", "user1", ttl_days=1)
        conn = _make_conn(_in_memory_db)
        two_days_ago = time.time() - 2 * 86400
        conn.execute(
            "UPDATE working_memory SET accessed_at=? WHERE memory_id=?",
            (two_days_ago, "m1"),
        )
        conn.commit()
        conn.close()
        s = stats()
        assert s["total_items"] == 1
        assert s["expired_pending_gc"] == 1
