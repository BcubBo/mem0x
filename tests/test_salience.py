"""salience 单元测试 — 注册、访问热度、批量操作。"""
import os
import sqlite3
import tempfile
import time
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def salience_db(monkeypatch):
    """创建临时 salience SQLite 数据库。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = tmp.name
    tmp.close()

    # Patch db_common to use temp path
    monkeypatch.setattr(
        "security.db_common.get_db_path",
        lambda name: tmp_path,
    )

    # Reset schema check
    import wrapper.salience as sal
    sal._schema_checked["salience"] = False

    # Patch _get_config
    monkeypatch.setattr(
        "wrapper.salience._get_config",
        lambda: {"initial_value": 0.5, "access_boost": 0.1},
    )

    # Patch _compute_fsrs_retrievability to always return None
    monkeypatch.setattr(
        "wrapper.salience._compute_fsrs_retrievability",
        lambda mid: None,
    )

    yield tmp_path

    try:
        os.unlink(tmp_path)
    except OSError:
        pass


class TestSalienceRegister:
    def test_register_basic(self, salience_db):
        """基本注册。"""
        from wrapper.salience import register
        register("mem1", content_preview="hello")
        from wrapper.salience import get_salience
        s = get_salience("mem1")
        assert s == 0.5

    def test_register_custom_initial(self, salience_db):
        """自定义初始值。"""
        from wrapper.salience import register, get_salience
        register("mem1", initial_salience=0.8)
        assert get_salience("mem1") == 0.8

    def test_register_replaces(self, salience_db):
        """重复注册覆盖。"""
        from wrapper.salience import register, get_salience
        register("mem1", initial_salience=0.3)
        register("mem1", initial_salience=0.9)
        assert get_salience("mem1") == 0.9

    def test_register_multiple(self, salience_db):
        """注册多个。"""
        from wrapper.salience import register, get_salience
        register("mem1")
        register("mem2")
        register("mem3")
        assert get_salience("mem1") == 0.5
        assert get_salience("mem2") == 0.5
        assert get_salience("mem3") == 0.5


class TestSalienceGet:
    def test_get_nonexistent(self, salience_db):
        """获取不存在的返回默认值。"""
        from wrapper.salience import get_salience
        assert get_salience("nonexistent") == 0.5

    def test_get_batch_empty(self, salience_db):
        """批量获取空列表。"""
        from wrapper.salience import get_batch_salience
        assert get_batch_salience([]) == {}

    def test_get_batch(self, salience_db):
        """批量获取。"""
        from wrapper.salience import register, get_batch_salience
        register("mem1", initial_salience=0.8)
        register("mem2", initial_salience=0.3)
        result = get_batch_salience(["mem1", "mem2", "mem3"])
        assert result["mem1"] == 0.8
        assert result["mem2"] == 0.3
        assert result["mem3"] == 0.5  # default

    def test_get_batch_partial(self, salience_db):
        """批量获取部分存在。"""
        from wrapper.salience import register, get_batch_salience
        register("mem1", initial_salience=0.9)
        result = get_batch_salience(["mem1", "missing"])
        assert "mem1" in result
        assert "missing" in result


class TestSalienceAccessed:
    def test_on_accessed_existing(self, salience_db):
        """访问已存在的记忆 boost salience。"""
        from wrapper.salience import register, on_memory_accessed, get_salience
        register("mem1", initial_salience=0.5)
        time.sleep(0.01)
        new_s = on_memory_accessed("mem1")
        assert new_s >= 0.5
        assert get_salience("mem1") == new_s

    def test_on_accessed_nonexistent(self, salience_db):
        """访问不存在的记忆创建新记录。"""
        from wrapper.salience import on_memory_accessed, get_salience
        new_s = on_memory_accessed("mem1")
        assert new_s == 0.5
        assert get_salience("mem1") == 0.5

    def test_on_accessed_increments_count(self, salience_db):
        """访问次数应递增。"""
        from wrapper.salience import register, on_memory_accessed
        register("mem1")
        on_memory_accessed("mem1")
        on_memory_accessed("mem1")
        # Check access_count via direct DB query
        conn = sqlite3.connect(salience_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT access_count FROM salience WHERE memory_id='mem1'").fetchone()
        conn.close()
        assert row["access_count"] == 2

    def test_on_accessed_boost_capped_at_1(self, salience_db):
        """salience 不应超过 1.0。"""
        from wrapper.salience import register, on_memory_accessed, get_salience
        register("mem1", initial_salience=0.95)
        for _ in range(10):
            on_memory_accessed("mem1")
        assert get_salience("mem1") <= 1.0


class TestSalienceBatch:
    def test_batch_on_accessed(self, salience_db):
        """批量更新。"""
        from wrapper.salience import register, batch_on_memory_accessed, get_batch_salience
        register("mem1", initial_salience=0.3)
        register("mem2", initial_salience=0.4)
        time.sleep(0.01)
        batch_on_memory_accessed(["mem1", "mem2"])
        result = get_batch_salience(["mem1", "mem2"])
        assert result["mem1"] > 0.3
        assert result["mem2"] > 0.4

    def test_batch_on_accessed_new_ids(self, salience_db):
        """批量更新包含新 ID。"""
        from wrapper.salience import batch_on_memory_accessed, get_salience
        batch_on_memory_accessed(["new1", "new2"])
        assert get_salience("new1") == 0.5
        assert get_salience("new2") == 0.5

    def test_batch_on_accessed_empty(self, salience_db):
        """空列表批量更新。"""
        from wrapper.salience import batch_on_memory_accessed
        batch_on_memory_accessed([])

    def test_batch_on_accessed_with_none(self, salience_db):
        """批量更新包含 None。"""
        from wrapper.salience import batch_on_memory_accessed, get_salience
        batch_on_memory_accessed([None, "valid", None])
        assert get_salience("valid") == 0.5


class TestSalienceDelete:
    def test_delete_existing(self, salience_db):
        """删除存在的记录。"""
        from wrapper.salience import register, delete, get_salience
        register("mem1")
        delete("mem1")
        assert get_salience("mem1") == 0.5  # returns default

    def test_delete_nonexistent(self, salience_db):
        """删除不存在的不报错。"""
        from wrapper.salience import delete
        delete("nonexistent")


class TestSalienceBoostResults:
    def test_boost_empty_results(self, salience_db):
        """空结果列表。"""
        from wrapper.salience import boost_salience_for_results
        assert boost_salience_for_results([]) == []

    def test_boost_no_ids(self, salience_db):
        """结果没有 id 字段。"""
        from wrapper.salience import boost_salience_for_results
        results = [{"memory": "test"}]
        assert boost_salience_for_results(results) == results

    def test_boost_injects_heat(self, salience_db):
        """注入 heat 分数。"""
        from wrapper.salience import register, boost_salience_for_results
        register("mem1", initial_salience=0.8)
        results = [{"id": "mem1", "memory": "test"}]
        boosted = boost_salience_for_results(results)
        assert "heat" in boosted[0]

    def test_boost_fsrs_fallback(self, salience_db):
        """无 fsrs_card 时回退到简单 boost。"""
        from wrapper.salience import register, boost_salience_for_results
        register("mem1", initial_salience=0.3)
        results = [{"id": "mem1"}]
        boosted = boost_salience_for_results(results)
        assert boosted[0]["heat"] > 0.3


class TestSalienceConfig:
    def test_default_config_values(self, salience_db):
        """默认配置值。"""
        from wrapper.salience import DEFAULT_INITIAL, DEFAULT_ACCESS_BOOST, DEFAULT_DECAY_RATE
        assert DEFAULT_INITIAL == 0.5
        assert DEFAULT_ACCESS_BOOST == 0.1
        assert DEFAULT_DECAY_RATE == 0.023
