"""version_tracker 单元测试 — 版本保存、查询、清理。"""
import os
import tempfile
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def vt_db(monkeypatch):
    """创建临时 version_tracker 数据库。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = tmp.name
    tmp.close()

    import wrapper.version_tracker as vt
    # Reset schema check
    vt._schema_checked["version_history"] = False

    monkeypatch.setattr(
        "security.db_common.get_db_path",
        lambda name: tmp_path,
    )

    yield tmp_path

    try:
        os.unlink(tmp_path)
    except OSError:
        pass


class TestVersionTrackerSave:
    def test_save_v1(self, vt_db):
        """保存第一个版本。"""
        from wrapper.version_tracker import save_version
        v = save_version("mem1", "first version")
        assert v == 1

    def test_save_increments(self, vt_db):
        """版本号递增。"""
        from wrapper.version_tracker import save_version
        v1 = save_version("mem1", "v1")
        v2 = save_version("mem1", "v2")
        v3 = save_version("mem1", "v3")
        assert v1 == 1
        assert v2 == 2
        assert v3 == 3

    def test_save_with_metadata(self, vt_db):
        """保存带 metadata。"""
        from wrapper.version_tracker import save_version
        v = save_version("mem1", "content", metadata={"key": "value"})
        assert v == 1

    def test_save_with_reason(self, vt_db):
        """保存带原因。"""
        from wrapper.version_tracker import save_version
        v = save_version("mem1", "content", reason="update")
        assert v == 1

    def test_save_multiple_memories(self, vt_db):
        """多个记忆的版本。"""
        from wrapper.version_tracker import save_version
        save_version("mem1", "v1")
        save_version("mem2", "v1")
        save_version("mem1", "v2")
        from wrapper.version_tracker import get_version_count
        assert get_version_count("mem1") == 2
        assert get_version_count("mem2") == 1


class TestVersionTrackerGetVersions:
    def test_get_versions_empty(self, vt_db):
        """空版本列表。"""
        from wrapper.version_tracker import get_versions
        assert get_versions("mem1") == []

    def test_get_versions(self, vt_db):
        """获取版本列表（最新在前）。"""
        from wrapper.version_tracker import save_version, get_versions
        save_version("mem1", "v1")
        save_version("mem1", "v2")
        save_version("mem1", "v3")
        versions = get_versions("mem1")
        assert len(versions) == 3
        assert versions[0]["version"] == 3
        assert versions[0]["content"] == "v3"

    def test_get_versions_with_limit(self, vt_db):
        """限制返回数量。"""
        from wrapper.version_tracker import save_version, get_versions
        for i in range(10):
            save_version("mem1", f"v{i}")
        versions = get_versions("mem1", limit=3)
        assert len(versions) == 3

    def test_get_versions_metadata(self, vt_db):
        """版本包含 metadata。"""
        from wrapper.version_tracker import save_version, get_versions
        save_version("mem1", "content", metadata={"key": "value"})
        versions = get_versions("mem1")
        assert versions[0]["metadata"] == {"key": "value"}


class TestVersionTrackerCount:
    def test_get_version_count(self, vt_db):
        """版本计数。"""
        from wrapper.version_tracker import save_version, get_version_count
        save_version("mem1", "v1")
        save_version("mem1", "v2")
        assert get_version_count("mem1") == 2

    def test_get_version_count_empty(self, vt_db):
        """空版本计数。"""
        from wrapper.version_tracker import get_version_count
        assert get_version_count("mem1") == 0

    def test_get_total_versions(self, vt_db):
        """总版本计数。"""
        from wrapper.version_tracker import save_version, get_total_versions
        save_version("mem1", "v1")
        save_version("mem2", "v1")
        assert get_total_versions() == 2


class TestVersionTrackerContent:
    def test_get_version_content(self, vt_db):
        """获取指定版本内容。"""
        from wrapper.version_tracker import save_version, get_version_content
        save_version("mem1", "v1")
        save_version("mem1", "v2")
        result = get_version_content("mem1", 2)
        assert result is not None
        assert result["content"] == "v2"
        assert result["version"] == 2

    def test_get_version_content_nonexistent(self, vt_db):
        """获取不存在的版本。"""
        from wrapper.version_tracker import get_version_content
        assert get_version_content("mem1", 999) is None


class TestVersionTrackerCleanup:
    def test_cleanup(self, vt_db):
        """清理所有版本。"""
        from wrapper.version_tracker import save_version, cleanup, get_version_count
        save_version("mem1", "v1")
        save_version("mem1", "v2")
        deleted = cleanup("mem1")
        assert deleted == 2
        assert get_version_count("mem1") == 0

    def test_cleanup_only_target(self, vt_db):
        """清理只影响目标记忆。"""
        from wrapper.version_tracker import save_version, cleanup, get_version_count
        save_version("mem1", "v1")
        save_version("mem2", "v1")
        cleanup("mem1")
        assert get_version_count("mem1") == 0
        assert get_version_count("mem2") == 1

    def test_cleanup_nonexistent(self, vt_db):
        """清理不存在的记忆。"""
        from wrapper.version_tracker import cleanup
        deleted = cleanup("nonexistent")
        assert deleted == 0
