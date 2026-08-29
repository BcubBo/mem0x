"""core_memory 单元测试 — 核心记忆 CRUD。"""
import os
import tempfile
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def core_db(monkeypatch):
    """创建临时 core_memory 数据库。"""
    import wrapper.core_memory as cm
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = tmp.name
    tmp.close()

    # Patch _get_db_path
    cm._db_path = tmp_path

    yield tmp_path

    cm._db_path = None
    try:
        os.unlink(tmp_path)
    except OSError:
        pass


class TestCoreMemoryAdd:
    def test_add_basic(self, core_db):
        """基本添加。"""
        from wrapper.core_memory import add_core_memory
        assert add_core_memory("mem1", "hello world") is True

    def test_add_with_category(self, core_db):
        """添加带分类。"""
        from wrapper.core_memory import add_core_memory
        add_core_memory("mem1", "content", category="project", importance=0.8)

    def test_add_replaces(self, core_db):
        """重复 memory_id 覆盖。"""
        from wrapper.core_memory import add_core_memory, get_core_memory
        add_core_memory("mem1", "old content")
        add_core_memory("mem1", "new content")
        result = get_core_memory("mem1")
        assert result["content"] == "new content"

    def test_add_multiple(self, core_db):
        """添加多条。"""
        from wrapper.core_memory import add_core_memory, list_core_memories
        add_core_memory("mem1", "content1")
        add_core_memory("mem2", "content2")
        assert len(list_core_memories()) == 2


class TestCoreMemoryRemove:
    def test_remove_existing(self, core_db):
        """移除存在的。"""
        from wrapper.core_memory import add_core_memory, remove_core_memory, is_core_memory
        add_core_memory("mem1", "content")
        assert is_core_memory("mem1") is True
        remove_core_memory("mem1")
        assert is_core_memory("mem1") is False

    def test_remove_nonexistent(self, core_db):
        """移除不存在的不报错。"""
        from wrapper.core_memory import remove_core_memory
        remove_core_memory("nonexistent")


class TestCoreMemoryIsCore:
    def test_is_core_memory_true(self, core_db):
        """检查核心记忆。"""
        from wrapper.core_memory import add_core_memory, is_core_memory
        add_core_memory("mem1", "content")
        assert is_core_memory("mem1") is True

    def test_is_core_memory_false(self, core_db):
        """不存在的不是核心记忆。"""
        from wrapper.core_memory import is_core_memory
        assert is_core_memory("nonexistent") is False


class TestCoreMemoryList:
    def test_list_all(self, core_db):
        """列出所有核心记忆。"""
        from wrapper.core_memory import add_core_memory, list_core_memories
        add_core_memory("mem1", "c1", importance=0.5)
        add_core_memory("mem2", "c2", importance=0.9)
        items = list_core_memories()
        assert len(items) == 2
        # Should be ordered by importance DESC
        assert items[0]["importance"] >= items[1]["importance"]

    def test_list_by_category(self, core_db):
        """按分类过滤。"""
        from wrapper.core_memory import add_core_memory, list_core_memories
        add_core_memory("mem1", "c1", category="project")
        add_core_memory("mem2", "c2", category="identity")
        items = list_core_memories(category="project")
        assert len(items) == 1
        assert items[0]["memory_id"] == "mem1"

    def test_list_empty(self, core_db):
        """空列表。"""
        from wrapper.core_memory import list_core_memories
        assert list_core_memories() == []

    def test_list_with_limit(self, core_db):
        """限制返回数量。"""
        from wrapper.core_memory import add_core_memory, list_core_memories
        for i in range(10):
            add_core_memory(f"mem{i}", f"content{i}")
        items = list_core_memories(limit=3)
        assert len(items) == 3


class TestCoreMemoryGet:
    def test_get_existing(self, core_db):
        """获取存在的核心记忆。"""
        from wrapper.core_memory import add_core_memory, get_core_memory
        add_core_memory("mem1", "hello world", category="project", importance=0.8)
        result = get_core_memory("mem1")
        assert result is not None
        assert result["content"] == "hello world"
        assert result["category"] == "project"
        assert result["importance"] == 0.8

    def test_get_nonexistent(self, core_db):
        """获取不存在的返回 None。"""
        from wrapper.core_memory import get_core_memory
        assert get_core_memory("nonexistent") is None


class TestCoreMemoryUpdateImportance:
    def test_update_importance(self, core_db):
        """更新重要性分数。"""
        from wrapper.core_memory import add_core_memory, get_core_memory, update_importance
        add_core_memory("mem1", "content", importance=0.5)
        update_importance("mem1", 0.95)
        result = get_core_memory("mem1")
        assert result["importance"] == 0.95
