"""hot_archive 单元测试 — 热知识归档。"""
import os
import sqlite3
import tempfile
import time
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def hot_archive_setup(monkeypatch):
    """创建临时环境。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = tmp.name
    tmp.close()

    # Patch config
    monkeypatch.setattr(
        "wrapper.hot_archive._get_config",
        lambda: {"salience_threshold": 0.5, "access_threshold": 3, "category": "hot_archive"},
    )

    yield tmp_path

    try:
        os.unlink(tmp_path)
    except OSError:
        pass


class TestHotArchiveConfig:
    def test_default_config(self):
        """默认配置值。"""
        from wrapper.hot_archive import (
            DEFAULT_INTERVAL, DEFAULT_SALIENCE_THRESHOLD,
            DEFAULT_ACCESS_THRESHOLD, DEFAULT_CATEGORY,
        )
        assert DEFAULT_INTERVAL == 3600 * 6
        assert DEFAULT_SALIENCE_THRESHOLD == 0.8
        assert DEFAULT_ACCESS_THRESHOLD == 5
        assert DEFAULT_CATEGORY == "hot_archive"


class TestFindHotCandidates:
    def test_find_with_db(self, monkeypatch, tmp_path):
        """有 DB 时查找候选。"""
        import wrapper.hot_archive as ha
        from wrapper import core_memory

        # Setup salience DB
        sal_db = str(tmp_path / "salience.db")
        conn = sqlite3.connect(sal_db)
        conn.execute("""
            CREATE TABLE salience (
                memory_id TEXT PRIMARY KEY,
                salience REAL NOT NULL DEFAULT 0.5,
                last_access REAL NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                content_preview TEXT DEFAULT ''
            )
        """)
        # Insert high-salience record
        now = time.time()
        conn.execute(
            "INSERT INTO salience VALUES (?, ?, ?, ?, ?, ?)",
            ("mem1", 0.9, now, 10, now, "high quality memory"),
        )
        conn.commit()
        conn.close()

        # Patch get_data_dir
        monkeypatch.setattr(
            "security.utils.get_data_dir",
            lambda: str(tmp_path),
        )
        # Patch core_memory to use temp DB
        core_db = str(tmp_path / "core_memory.db")
        core_memory._db_path = core_db
        core_memory._ensure_db()

        # Also patch salience DB path
        monkeypatch.setattr(
            "security.db_common.get_db_path",
            lambda name: sal_db if name == "salience" else str(tmp_path / f"{name}.db"),
        )

        candidates = ha.find_hot_candidates()
        # mem1 should be a candidate (high salience + high access, not core)
        assert any(c["memory_id"] == "mem1" for c in candidates)
        core_memory._db_path = None

    def test_find_excludes_core_memories(self, monkeypatch, tmp_path):
        """排除已核心化的记忆。"""
        import wrapper.hot_archive as ha
        from wrapper import core_memory

        # Setup salience DB
        sal_db = str(tmp_path / "salience.db")
        conn = sqlite3.connect(sal_db)
        conn.execute("""
            CREATE TABLE salience (
                memory_id TEXT PRIMARY KEY,
                salience REAL NOT NULL DEFAULT 0.5,
                last_access REAL NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                content_preview TEXT DEFAULT ''
            )
        """)
        now = time.time()
        conn.execute(
            "INSERT INTO salience VALUES (?, ?, ?, ?, ?, ?)",
            ("mem_core", 0.9, now, 10, now, "core memory"),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("security.utils.get_data_dir", lambda: str(tmp_path))
        monkeypatch.setattr(
            "security.db_common.get_db_path",
            lambda name: sal_db if name == "salience" else str(tmp_path / f"{name}.db"),
        )

        # Mark mem_core as core memory
        core_db = str(tmp_path / "core_memory.db")
        core_memory._db_path = core_db
        core_memory._ensure_db()
        core_memory.add_core_memory("mem_core", "content")

        candidates = ha.find_hot_candidates()
        assert not any(c["memory_id"] == "mem_core" for c in candidates)
        core_memory._db_path = None

    def test_find_no_db(self, monkeypatch):
        """DB 不存在时返回空。"""
        import wrapper.hot_archive as ha
        monkeypatch.setattr(
            "security.utils.get_data_dir",
            lambda: "/nonexistent/path",
        )
        candidates = ha.find_hot_candidates()
        assert candidates == []


class TestArchiveHotMemories:
    def test_archive_basic(self, monkeypatch):
        """基本归档。"""
        from wrapper import core_memory
        from wrapper.hot_archive import archive_hot_memories
        import tempfile

        core_db = tempfile.mktemp(suffix=".db")
        core_memory._db_path = core_db
        core_memory._ensure_db()

        candidates = [
            {"memory_id": "mem1", "salience": 0.9, "access_count": 10, "content_preview": "test"},
        ]
        result = archive_hot_memories(candidates)
        assert result["archived"] == 1
        assert result["errors"] == 0
        core_memory._db_path = None
        try:
            os.unlink(core_db)
        except OSError:
            pass

    def test_archive_empty(self):
        """空候选列表。"""
        from wrapper.hot_archive import archive_hot_memories
        result = archive_hot_memories([])
        assert result["archived"] == 0
        assert result["total_candidates"] == 0

    def test_archive_importance_mapping(self, monkeypatch):
        """importance 映射计算。"""
        from wrapper import core_memory
        from wrapper.hot_archive import archive_hot_memories
        import tempfile

        core_db = tempfile.mktemp(suffix=".db")
        core_memory._db_path = core_db
        core_memory._ensure_db()

        candidates = [
            {"memory_id": "mem1", "salience": 0.8, "access_count": 5, "content_preview": "test"},
        ]
        result = archive_hot_memories(candidates)
        # importance = min(1.0, 0.5 + 0.8 * 0.5) = 0.9
        assert result["archived"] == 1
        core_memory._db_path = None
        try:
            os.unlink(core_db)
        except OSError:
            pass


class TestRunArchiveCycle:
    def test_run_cycle_no_candidates(self, monkeypatch):
        """无候选时的完整周期。"""
        import wrapper.hot_archive as ha
        monkeypatch.setattr(ha, "find_hot_candidates", lambda: [])
        result = ha.run_archive_cycle()
        assert result["archived"] == 0
        assert result["elapsed_ms"] >= 0


class TestHotArchiveThread:
    def test_is_running_initial(self):
        """初始状态。"""
        import wrapper.hot_archive as ha
        ha._running = False
        ha._thread = None
        assert ha.is_running() is False

    def test_start_stop(self):
        """启动和停止。"""
        import wrapper.hot_archive as ha
        ha._running = False
        ha._thread = None
        ha._stop_event.clear()
        ha.start()
        time.sleep(0.1)
        # stop
        ha.stop()
        time.sleep(0.5)
        assert ha._running is False
