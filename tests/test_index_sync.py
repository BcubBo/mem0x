"""index_sync 单元测试 — 删除/合并后同步 FTS5/salience/vt。"""
import os
import tempfile
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def index_sync_env(monkeypatch, tmp_path):
    """设置 index_sync 测试环境。"""
    # Patch fts5_store to use temp DB
    from wrapper.fts5_store import FTS5Store
    fts5_db = str(tmp_path / "fts5.db")
    config = {
        "db_path": fts5_db,
        "tokenizer": "unicode61",
        "bm25_weight": 0.3,
        "highlight": True,
        "hot_words": False,
        "query_log": False,
        "extract_tags": False,
        "search_limit": 20,
        "hot_words_min_length": 2,
    }
    fts5 = FTS5Store(config=config)

    # Patch get_fts5
    import wrapper.fts5_store as fts5_mod
    original_fts5 = fts5_mod._fts5_instance
    fts5_mod._fts5_instance = fts5

    # Patch salience
    import wrapper.salience as sal_mod
    sal_db = str(tmp_path / "salience.db")
    monkeypatch.setattr("security.db_common.get_db_path", lambda name: sal_db)
    sal_mod._schema_checked["salience"] = False
    monkeypatch.setattr("wrapper.salience._get_config", lambda: {})
    monkeypatch.setattr("wrapper.salience._compute_fsrs_retrievability", lambda mid: None)

    # Patch version_tracker
    import wrapper.version_tracker as vt_mod
    vt_db = str(tmp_path / "vt.db")
    vt_mod._schema_checked["version_history"] = False

    # Patch compensation
    monkeypatch.setattr(
        "security.compensation.enqueue",
        mock.MagicMock(),
    )

    yield {"fts5": fts5, "sal_db": sal_db, "vt_db": vt_db, "tmp_path": tmp_path}

    # Cleanup
    fts5_mod._fts5_instance = original_fts5


class TestSyncAfterDelete:
    def test_sync_delete_basic(self, index_sync_env):
        """基本删除同步。"""
        from wrapper.index_sync import sync_after_delete
        from wrapper.fts5_store import FTS5Store

        # Write to FTS5 first
        fts5 = index_sync_env["fts5"]
        fts5.write("mem1", "hello world", user_id="u1")

        sync_after_delete("mem1", "u1")
        # mem1 should be removed from FTS5
        assert fts5.count() == 0

    def test_sync_delete_nonexistent(self, index_sync_env):
        """删除不存在的记录不报错。"""
        from wrapper.index_sync import sync_after_delete
        sync_after_delete("nonexistent", "u1")

    def test_sync_delete_only_removes_target(self, index_sync_env):
        """只删除目标记录。"""
        from wrapper.index_sync import sync_after_delete
        fts5 = index_sync_env["fts5"]
        fts5.write("mem1", "first", user_id="u1")
        fts5.write("mem2", "second", user_id="u1")

        sync_after_delete("mem1", "u1")
        assert fts5.count() == 1

    def test_sync_delete_cleans_salience(self, index_sync_env):
        """删除时清理 salience。"""
        from wrapper.index_sync import sync_after_delete
        from wrapper import salience
        salience.register("mem1", content_preview="test")
        sync_after_delete("mem1", "u1")
        assert salience.get_salience("mem1") == salience.DEFAULT_INITIAL


class TestSyncAfterMerge:
    def test_sync_merge_basic(self, index_sync_env):
        """基本合并同步。"""
        from wrapper.index_sync import sync_after_merge
        fts5 = index_sync_env["fts5"]
        fts5.write("old1", "old content 1", user_id="u1")
        fts5.write("old2", "old content 2", user_id="u1")

        sync_after_merge("new1", ["old1", "old2"], "merged content", "u1")
        # Old IDs should be removed, new ID should exist
        assert fts5.count() == 1

    def test_sync_merge_empty_old_ids(self, index_sync_env):
        """空旧 ID 列表。"""
        from wrapper.index_sync import sync_after_merge
        fts5 = index_sync_env["fts5"]
        sync_after_merge("new1", [], "content", "u1")
        assert fts5.count() == 1

    def test_sync_merge_cleans_old_salience(self, index_sync_env):
        """合并清理旧 salience。"""
        from wrapper.index_sync import sync_after_merge
        from wrapper import salience
        salience.register("old1")
        salience.register("old2")
        sync_after_merge("new1", ["old1", "old2"], "content", "u1")
        # Old should be cleaned, new should be registered


class TestCompensateDelete:
    def test_compensate_delete(self, index_sync_env):
        """补偿删除。"""
        import asyncio
        from wrapper.index_sync import compensate_delete
        from wrapper.fts5_store import FTS5Store

        fts5 = index_sync_env["fts5"]
        fts5.write("mem1", "hello", user_id="u1")

        async def run():
            result = await compensate_delete("mem1", {"user_id": "u1"})
            return result

        result = asyncio.run(run())
        assert result["action"] == "ok"
        assert fts5.count() == 0


class TestCompensateMerge:
    def test_compensate_merge(self, index_sync_env):
        """补偿合并。"""
        import asyncio
        from wrapper.index_sync import compensate_merge
        from wrapper.fts5_store import FTS5Store

        fts5 = index_sync_env["fts5"]
        fts5.write("old1", "old", user_id="u1")

        async def run():
            result = await compensate_merge(
                "new1",
                {"user_id": "u1", "old_ids": ["old1"], "merged_text": "new"},
            )
            return result

        result = asyncio.run(run())
        assert result["action"] == "ok"
