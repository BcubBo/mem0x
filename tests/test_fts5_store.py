"""FTS5Store 单元测试 — 全文检索、热词、搜索历史、标签提取。"""
import os
import sqlite3
import tempfile
import time
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def fts5_store(tmp_path):
    """创建临时 FTS5Store 实例（使用临时 SQLite）。"""
    from wrapper.fts5_store import FTS5Store
    db_path = str(tmp_path / "test_fts5.db")
    config = {
        "db_path": db_path,
        "tokenizer": "unicode61",
        "bm25_weight": 0.3,
        "highlight": True,
        "hot_words": True,
        "query_log": True,
        "extract_tags": True,
        "search_limit": 20,
        "hot_words_min_length": 2,
    }
    return FTS5Store(config=config)


class TestFTS5Init:
    def test_tables_created(self, fts5_store):
        """表应被创建。"""
        conn = sqlite3.connect(fts5_store.db_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' OR type='view'"
        ).fetchall()]
        assert "fts5_meta" in tables
        assert "fts5_query_log" in tables
        assert "fts5_hot_words" in tables
        conn.close()

    def test_config_applied(self, fts5_store):
        """配置应被正确应用。"""
        assert fts5_store.bm25_weight == 0.3
        assert fts5_store.highlight_enabled is True
        assert fts5_store.search_limit == 20
        assert fts5_store.hot_words_min_len == 2


class TestFTS5Write:
    def test_write_basic(self, fts5_store):
        """基本写入。"""
        fts5_store.write("mem1", "hello world", user_id="user1")
        assert fts5_store.count() == 1

    def test_write_replaces(self, fts5_store):
        """重复 memory_id 应覆盖。"""
        fts5_store.write("mem1", "old content", user_id="user1")
        fts5_store.write("mem1", "new content", user_id="user1")
        assert fts5_store.count() == 1

    def test_write_multiple(self, fts5_store):
        """写入多条记录。"""
        fts5_store.write("mem1", "first memory", user_id="user1")
        fts5_store.write("mem2", "second memory", user_id="user2")
        assert fts5_store.count() == 2

    def test_write_with_metadata(self, fts5_store):
        """写入带 metadata。"""
        fts5_store.write("mem1", "content", user_id="u1", metadata='{"key":"val"}')
        assert fts5_store.count() == 1

    def test_count_by_user(self, fts5_store):
        """按 user_id 计数。"""
        fts5_store.write("mem1", "a", user_id="u1")
        fts5_store.write("mem2", "b", user_id="u1")
        fts5_store.write("mem3", "c", user_id="u2")
        assert fts5_store.count(user_id="u1") == 2
        assert fts5_store.count(user_id="u2") == 1


class TestFTS5Delete:
    def test_delete_basic(self, fts5_store):
        """删除记录。"""
        fts5_store.write("mem1", "hello world", user_id="u1")
        fts5_store.delete("mem1")
        assert fts5_store.count() == 0

    def test_delete_only_target(self, fts5_store):
        """删除只影响目标。"""
        fts5_store.write("mem1", "first", user_id="u1")
        fts5_store.write("mem2", "second", user_id="u1")
        fts5_store.delete("mem1")
        assert fts5_store.count() == 1

    def test_delete_nonexistent(self, fts5_store):
        """删除不存在的记录不报错。"""
        fts5_store.delete("nonexistent")


class TestFTS5Search:
    def test_search_basic(self, fts5_store):
        """基本搜索。"""
        fts5_store.write("mem1", "the quick brown fox jumps over the lazy dog", user_id="u1")
        results = fts5_store.search("quick fox")
        assert len(results) >= 1
        assert results[0]["memory_id"] == "mem1"

    def test_search_with_user_filter(self, fts5_store):
        """搜索时按 user_id 过滤。"""
        fts5_store.write("mem1", "hello world", user_id="u1")
        fts5_store.write("mem2", "hello there", user_id="u2")
        results = fts5_store.search("hello", user_id="u1")
        assert len(results) == 1
        assert results[0]["memory_id"] == "mem1"

    def test_search_empty_query(self, fts5_store):
        """空查询返回空。"""
        fts5_store.write("mem1", "hello", user_id="u1")
        assert fts5_store.search("") == []
        assert fts5_store.search("   ") == []

    def test_search_phrase(self, fts5_store):
        """短语搜索（带引号）。"""
        fts5_store.write("mem1", "the quick brown fox", user_id="u1")
        fts5_store.write("mem2", "the lazy brown dog", user_id="u1")
        results = fts5_store.search('"quick brown"')
        assert len(results) == 1
        assert results[0]["memory_id"] == "mem1"

    def test_search_no_highlight(self, fts5_store):
        """搜索时关闭高亮。"""
        fts5_store.write("mem1", "hello world", user_id="u1")
        results = fts5_store.search("hello", highlight=False)
        assert len(results) == 1
        assert "snippet" not in results[0]

    def test_search_no_match(self, fts5_store):
        """无匹配结果。"""
        fts5_store.write("mem1", "hello world", user_id="u1")
        results = fts5_store.search("xyz")
        assert len(results) == 0

    def test_search_logs_query(self, fts5_store):
        """搜索应记录到 query_log。"""
        fts5_store.write("mem1", "hello world", user_id="u1")
        fts5_store.search("hello")
        history = fts5_store.get_query_history()
        assert len(history) == 1
        assert history[0]["query"] == "hello"

    def test_search_updates_hot_words(self, fts5_store):
        """搜索应更新热词。"""
        fts5_store.write("mem1", "hello world", user_id="u1")
        fts5_store.search("hello world")
        hot_words = fts5_store.get_hot_words()
        assert len(hot_words) >= 1

    def test_search_limit(self, fts5_store):
        """搜索限制。"""
        for i in range(10):
            fts5_store.write(f"mem{i}", f"memory item {i} unique", user_id="u1")
        results = fts5_store.search("memory", limit=3)
        assert len(results) <= 3

    def test_search_non_ascii(self, fts5_store):
        """非 ASCII 搜索（英文+数字混合）。"""
        fts5_store.write("mem1", "test123 memory system version", user_id="u1")
        results = fts5_store.search("memory")
        assert len(results) >= 1


class TestFTS5QueryHistory:
    def test_get_query_history_empty(self, fts5_store):
        """空历史。"""
        assert fts5_store.get_query_history() == []

    def test_get_query_history_after_search(self, fts5_store):
        """搜索后有历史。"""
        fts5_store.write("mem1", "hello", user_id="u1")
        fts5_store.search("hello")
        history = fts5_store.get_query_history()
        assert len(history) == 1
        assert history[0]["results"] >= 0


class TestFTS5HotWords:
    def test_get_hot_words_empty(self, fts5_store):
        """空热词。"""
        assert fts5_store.get_hot_words() == []

    def test_hot_words_increments(self, fts5_store):
        """热词计数递增。"""
        fts5_store.write("mem1", "hello world test", user_id="u1")
        fts5_store.search("hello world test")
        fts5_store.search("hello world test")
        hot = fts5_store.get_hot_words()
        assert any(h["word"] == "hello" for h in hot)


class TestFTS5ExtractTags:
    def test_extract_tags_basic(self, fts5_store):
        """基本标签提取。"""
        tags = fts5_store.extract_tags("mem0 Qdrant Docker API plugin config test")
        assert isinstance(tags, list)
        assert len(tags) > 0

    def test_extract_tags_empty_content(self, fts5_store):
        """空内容返回空标签。"""
        assert fts5_store.extract_tags("") == []

    def test_extract_tags_disabled(self, tmp_path):
        """禁用标签提取。"""
        from wrapper.fts5_store import FTS5Store
        config = {
            "db_path": str(tmp_path / "no_tags.db"),
            "tokenizer": "unicode61",
            "bm25_weight": 0.3,
            "highlight": True,
            "hot_words": False,
            "query_log": False,
            "extract_tags": False,
            "search_limit": 20,
            "hot_words_min_length": 2,
        }
        store = FTS5Store(config=config)
        assert store.extract_tags("hello world test") == []

    def test_extract_tags_stop_words(self, fts5_store):
        """停用词应被过滤。"""
        tags = fts5_store.extract_tags("the a an is are was in on at to for of")
        assert len(tags) == 0


class TestFTS5Sync:
    def test_sync_from_qdrant(self, fts5_store):
        """从 Qdrant 同步。"""
        records = [
            {"id": "mem1", "memory": "first memory", "metadata": {"user_id": "u1"}},
            {"id": "mem2", "memory": "second memory", "metadata": {"user_id": "u2"}},
        ]
        count = fts5_store.sync_from_qdrant(records)
        assert count == 2
        assert fts5_store.count() == 2

    def test_sync_from_qdrant_with_content_field(self, fts5_store):
        """同步时使用 content 字段。"""
        records = [
            {"id": "mem1", "content": "content field memory", "metadata": {"user_id": "u1"}},
        ]
        count = fts5_store.sync_from_qdrant(records)
        assert count == 1

    def test_sync_from_qdrant_empty(self, fts5_store):
        """空列表同步。"""
        count = fts5_store.sync_from_qdrant([])
        assert count == 0

    def test_sync_replaces_existing(self, fts5_store):
        """同步覆盖已有记录。"""
        fts5_store.write("mem1", "old content", user_id="u1")
        records = [
            {"id": "mem1", "memory": "new content", "metadata": {"user_id": "u1"}},
        ]
        fts5_store.sync_from_qdrant(records)
        assert fts5_store.count() == 1


class TestFTS5Singleton:
    def test_get_fts5_returns_singleton(self):
        """get_fts5 返回单例。"""
        import wrapper.fts5_store as mod
        mod._fts5_instance = None
        s1 = mod.get_fts5()
        s2 = mod.get_fts5()
        assert s1 is s2
        mod._fts5_instance = None
