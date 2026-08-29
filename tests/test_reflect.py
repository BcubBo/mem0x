"""reflect 单元测试 — 反思日志、线程管理。"""
import os
import sqlite3
import tempfile
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def reflect_env(monkeypatch, tmp_path):
    """创建临时 reflect DB。"""
    import wrapper.reflect as rmod
    db_path = str(tmp_path / "reflect.db")
    monkeypatch.setattr("wrapper.reflect._get_db_path", lambda: db_path)
    rmod._db_path = db_path
    rmod._ensure_db()
    yield db_path
    rmod._db_path = None


class TestReflectDB:
    def test_init_db(self, reflect_env):
        """DB 初始化。"""
        conn = sqlite3.connect(reflect_env)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "reflect_logs" in tables
        conn.close()


class TestReflectLogs:
    def test_list_reflect_logs_empty(self, reflect_env):
        """空日志列表。"""
        from wrapper.reflect import list_reflect_logs
        assert list_reflect_logs() == []

    def test_list_reflect_logs_with_data(self, reflect_env):
        """有数据的日志列表。"""
        from wrapper.reflect import list_reflect_logs
        conn = sqlite3.connect(reflect_env)
        conn.execute(
            "INSERT INTO reflect_logs (total_memories, quality_score, issues, suggestions) VALUES (?, ?, ?, ?)",
            (100, 0.8, '["test"]', '["suggestion"]'),
        )
        conn.commit()
        conn.close()
        logs = list_reflect_logs()
        assert len(logs) == 1
        assert logs[0]["total_memories"] == 100

    def test_list_reflect_logs_limit(self, reflect_env):
        """日志列表限制。"""
        from wrapper.reflect import list_reflect_logs
        conn = sqlite3.connect(reflect_env)
        for i in range(10):
            conn.execute(
                "INSERT INTO reflect_logs (total_memories, quality_score, issues, suggestions) VALUES (?, ?, ?, ?)",
                (i, 0.5, '[]', '[]'),
            )
        conn.commit()
        conn.close()
        logs = list_reflect_logs(limit=3)
        assert len(logs) == 3


class TestReflectThread:
    def test_is_running_initial(self):
        """初始状态。"""
        import wrapper.reflect as rmod
        rmod._running = False
        rmod._thread = None
        assert rmod.is_running() is False

    def test_stop(self):
        """停止。"""
        import wrapper.reflect as rmod
        rmod._running = True
        rmod.stop()
        assert rmod._running is False

    def test_start_stop_cycle(self):
        """启动和停止循环。"""
        import wrapper.reflect as rmod
        rmod._running = False
        rmod._thread = None

        def mock_getter():
            return None

        rmod.start(mock_getter, interval=1)
        assert rmod._running is True
        rmod.stop()
        import time
        time.sleep(0.1)
        assert rmod._running is False


class TestReflectRunCycle:
    @pytest.mark.asyncio
    async def test_run_cycle_with_memory(self, reflect_env):
        """有 memory 的反思周期。"""
        from wrapper.reflect import run_reflect_cycle

        # Mock memory instance
        mock_memory = mock.AsyncMock()
        mock_memory.search = mock.AsyncMock(return_value={
            "results": [
                {"memory": "test memory", "score": 0.8, "created_at": "2026-08-20T00:00:00+00:00"},
                {"memory": "another memory", "score": 0.6, "created_at": "2026-08-28T00:00:00+00:00"},
            ]
        })

        result = await run_reflect_cycle(mock_memory, user_id="test_user")
        assert result["status"] == "ok"
        assert "health" in result

    @pytest.mark.asyncio
    async def test_run_cycle_empty_memory(self, reflect_env):
        """空记忆库的反思周期。"""
        from wrapper.reflect import run_reflect_cycle

        mock_memory = mock.AsyncMock()
        mock_memory.search = mock.AsyncMock(return_value={"results": []})

        result = await run_reflect_cycle(mock_memory, user_id="test_user")
        assert result["status"] == "ok"
        assert "记忆库为空" in result["health"]["issues"]


class TestReflectAnalyzeHealth:
    @pytest.mark.asyncio
    async def test_analyze_health_empty(self, reflect_env):
        """分析空系统。"""
        from wrapper.reflect import analyze_system_health

        mock_memory = mock.AsyncMock()
        mock_memory.search = mock.AsyncMock(return_value={"results": []})

        health = await analyze_system_health(mock_memory, user_id="test")
        assert health["total_memories"] == 0
        assert health["quality_score"] == 0.0
        assert "记忆库为空" in health["issues"]

    @pytest.mark.asyncio
    async def test_analyze_health_low_scores(self, reflect_env):
        """低质量记忆。"""
        from wrapper.reflect import analyze_system_health

        # Create mock results with many low scores
        results = [
            {"memory": f"mem{i}", "score": 0.1, "created_at": "2026-01-01T00:00:00+00:00"}
            for i in range(20)
        ]
        mock_memory = mock.AsyncMock()
        mock_memory.search = mock.AsyncMock(return_value={"results": results})

        health = await analyze_system_health(mock_memory, user_id="test")
        assert len(health["issues"]) > 0

    @pytest.mark.asyncio
    async def test_analyze_health_concurrent_block(self, reflect_env):
        """并发分析被阻止。"""
        from wrapper.reflect import analyze_system_health, _health_lock

        mock_memory = mock.AsyncMock()
        mock_memory.search = mock.AsyncMock(return_value={"results": []})

        # Acquire lock to simulate concurrent
        _health_lock.acquire()
        try:
            health = await analyze_system_health(mock_memory, user_id="test")
            assert health["issues"] == ["分析正在进行中"]
        finally:
            _health_lock.release()
