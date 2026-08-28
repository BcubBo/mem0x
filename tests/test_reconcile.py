"""reconcile 单元测试 — 三库对账逻辑。

mock Qdrant / FTS5 / salience 收集函数，测试对账场景 A-D。
"""
import os
import sys
import time
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wrapper.reconcile as rec


@pytest.fixture(autouse=True)
def _reset_state():
    """每个测试重置 reconcile 模块状态。"""
    rec._last_run = None
    rec._last_result = None
    rec._running = False
    yield
    rec._running = False


# ──────────────────────────────────────────────
# 辅助：构造 mock 数据
# ──────────────────────────────────────────────

def _make_point(pid, deleted=False, content="text", user_id="bo"):
    """构造一个 Qdrant point mock。"""
    payload = {"data": content, "user_id": user_id}
    if deleted:
        payload["deleted_at"] = time.time()
    return SimpleNamespace(id=pid, payload=payload)


# ──────────────────────────────────────────────
# 场景 A：Qdrant 已标 deleted + FTS5/salience 仍有 → 清理孤儿
# ──────────────────────────────────────────────

class TestScenarioA_CleanOrphans:
    def test_cleans_deleted_in_fts5(self, monkeypatch):
        """Qdrant deleted + FTS5 有 → 清理 FTS5 孤儿。"""
        qdrant_active = {"a1", "a2"}
        qdrant_deleted = {"d1"}
        fts5_ids = {"a1", "a2", "d1"}  # d1 is orphan in FTS5
        salience_ids = {"a1"}

        mock_fts5 = mock.MagicMock()
        mock_fts5.delete = mock.MagicMock()

        monkeypatch.setattr(rec, "_collect_qdrant_ids", lambda: (qdrant_active, qdrant_deleted))
        monkeypatch.setattr(rec, "_collect_fts5_ids", lambda: fts5_ids)
        monkeypatch.setattr(rec, "_collect_salience_ids", lambda: salience_ids)
        monkeypatch.setattr(rec, "_load_config", lambda: {})
        monkeypatch.setattr(rec, "_fetch_qdrant_content", lambda ids: {})

        with mock.patch("wrapper.fts5_store.get_fts5", return_value=mock_fts5):
            with mock.patch("wrapper.fts5_store.get_fts5", return_value=mock_fts5, create=True):
                result = rec.reconcile_all()

        assert result["orphan_cleaned"]["fts5"] == 1
        mock_fts5.delete.assert_called_once_with("d1")

    def test_cleans_deleted_in_salience(self, monkeypatch):
        """Qdrant deleted + salience 有 → 清理 salience 孤儿。"""
        qdrant_active = {"a1"}
        qdrant_deleted = {"d1"}
        fts5_ids = {"a1"}
        salience_ids = {"a1", "d1"}  # d1 is orphan in salience

        mock_sal_delete = mock.MagicMock()

        monkeypatch.setattr(rec, "_collect_qdrant_ids", lambda: (qdrant_active, qdrant_deleted))
        monkeypatch.setattr(rec, "_collect_fts5_ids", lambda: fts5_ids)
        monkeypatch.setattr(rec, "_collect_salience_ids", lambda: salience_ids)
        monkeypatch.setattr(rec, "_load_config", lambda: {})
        monkeypatch.setattr(rec, "_fetch_qdrant_content", lambda ids: {})

        with mock.patch("wrapper.salience.delete", mock_sal_delete):
            result = rec.reconcile_all()

        assert result["orphan_cleaned"]["salience"] == 1
        mock_sal_delete.assert_called_once_with("d1")


# ──────────────────────────────────────────────
# 场景 B：Qdrant active 但 FTS5 缺失 → 告警或回填
# ──────────────────────────────────────────────

class TestScenarioB_MissingFTS5:
    def test_warns_when_no_auto_backfill(self, monkeypatch):
        """FTS5 缺失且 auto_backfill=False → 仅告警。"""
        qdrant_active = {"a1", "a2", "a3"}
        qdrant_deleted = set()
        fts5_ids = {"a1"}  # a2, a3 missing
        salience_ids = {"a1", "a2", "a3"}

        monkeypatch.setattr(rec, "_collect_qdrant_ids", lambda: (qdrant_active, qdrant_deleted))
        monkeypatch.setattr(rec, "_collect_fts5_ids", lambda: fts5_ids)
        monkeypatch.setattr(rec, "_collect_salience_ids", lambda: salience_ids)
        monkeypatch.setattr(rec, "_load_config", lambda: {"auto_backfill_fts5": False})
        monkeypatch.setattr(rec, "_fetch_qdrant_content", lambda ids: {})

        result = rec.reconcile_all()

        assert result["warnings"]["missing_fts5"] == 2
        assert result["fts5_backfilled"] == 0

    def test_auto_backfills_when_configured(self, monkeypatch):
        """auto_backfill_fts5=True → 自动回填缺失的 FTS5。"""
        qdrant_active = {"a1", "a2"}
        qdrant_deleted = set()
        fts5_ids = {"a1"}  # a2 missing
        salience_ids = {"a1", "a2"}

        mock_fts5 = mock.MagicMock()

        monkeypatch.setattr(rec, "_collect_qdrant_ids", lambda: (qdrant_active, qdrant_deleted))
        monkeypatch.setattr(rec, "_collect_fts5_ids", lambda: fts5_ids)
        monkeypatch.setattr(rec, "_collect_salience_ids", lambda: salience_ids)
        monkeypatch.setattr(rec, "_load_config", lambda: {"auto_backfill_fts5": True})
        monkeypatch.setattr(
            rec, "_fetch_qdrant_content",
            lambda ids: {"a2": {"content": "hello", "user_id": "bo"}},
        )

        with mock.patch("wrapper.fts5_store.get_fts5", return_value=mock_fts5):
            result = rec.reconcile_all()

        assert result["warnings"]["missing_fts5"] == 1
        assert result["fts5_backfilled"] == 1
        mock_fts5.write.assert_called_once_with("a2", "hello", "bo")


# ──────────────────────────────────────────────
# 场景 C：FTS5 存在但 Qdrant 不存在 → 孤儿告警
# ──────────────────────────────────────────────

class TestScenarioC_OrphanFTS5:
    def test_detects_orphan_fts5(self, monkeypatch):
        """FTS5 有但 Qdrant 全无 → orphan_fts5 告警。"""
        qdrant_active = {"a1"}
        qdrant_deleted = {"d1"}
        fts5_ids = {"a1", "orphan1", "orphan2"}  # orphan1, orphan2 not in Qdrant at all
        salience_ids = {"a1"}

        monkeypatch.setattr(rec, "_collect_qdrant_ids", lambda: (qdrant_active, qdrant_deleted))
        monkeypatch.setattr(rec, "_collect_fts5_ids", lambda: fts5_ids)
        monkeypatch.setattr(rec, "_collect_salience_ids", lambda: salience_ids)
        monkeypatch.setattr(rec, "_load_config", lambda: {})
        monkeypatch.setattr(rec, "_fetch_qdrant_content", lambda ids: {})

        result = rec.reconcile_all()

        assert result["warnings"]["orphan_fts5"] == 2
        assert "orphan1" in result["warnings"]["orphan_fts5_sample"]
        assert "orphan2" in result["warnings"]["orphan_fts5_sample"]


# ──────────────────────────────────────────────
# 场景 D：salience 存在但 Qdrant 不存在 → 孤儿告警
# ──────────────────────────────────────────────

class TestScenarioD_OrphanSalience:
    def test_detects_orphan_salience(self, monkeypatch):
        """salience 有但 Qdrant 全无 → orphan_salience 告警。"""
        qdrant_active = {"a1"}
        qdrant_deleted = set()
        fts5_ids = {"a1"}
        salience_ids = {"a1", "orphan_s1"}

        monkeypatch.setattr(rec, "_collect_qdrant_ids", lambda: (qdrant_active, qdrant_deleted))
        monkeypatch.setattr(rec, "_collect_fts5_ids", lambda: fts5_ids)
        monkeypatch.setattr(rec, "_collect_salience_ids", lambda: salience_ids)
        monkeypatch.setattr(rec, "_load_config", lambda: {})
        monkeypatch.setattr(rec, "_fetch_qdrant_content", lambda ids: {})

        result = rec.reconcile_all()

        assert result["warnings"]["orphan_salience"] == 1
        assert "orphan_s1" in result["warnings"]["orphan_salience_sample"]


# ──────────────────────────────────────────────
# 全量一致（无差异）
# ──────────────────────────────────────────────

class TestReconcileAllClean:
    def test_no_discrepancies(self, monkeypatch):
        """三端一致 → status=ok，无告警，无清理。"""
        ids = {"a1", "a2", "a3"}
        monkeypatch.setattr(rec, "_collect_qdrant_ids", lambda: (ids, set()))
        monkeypatch.setattr(rec, "_collect_fts5_ids", lambda: ids.copy())
        monkeypatch.setattr(rec, "_collect_salience_ids", lambda: ids.copy())
        monkeypatch.setattr(rec, "_load_config", lambda: {})
        monkeypatch.setattr(rec, "_fetch_qdrant_content", lambda x: {})

        result = rec.reconcile_all()

        assert result["status"] == "ok"
        assert result["orphan_cleaned"]["fts5"] == 0
        assert result["orphan_cleaned"]["salience"] == 0
        assert result["warnings"]["missing_fts5"] == 0
        assert result["warnings"]["orphan_fts5"] == 0
        assert result["warnings"]["orphan_salience"] == 0

    def test_empty_stores(self, monkeypatch):
        """三端全空 → 正常完成。"""
        monkeypatch.setattr(rec, "_collect_qdrant_ids", lambda: (set(), set()))
        monkeypatch.setattr(rec, "_collect_fts5_ids", lambda: set())
        monkeypatch.setattr(rec, "_collect_salience_ids", lambda: set())
        monkeypatch.setattr(rec, "_load_config", lambda: {})
        monkeypatch.setattr(rec, "_fetch_qdrant_content", lambda x: {})

        result = rec.reconcile_all()
        assert result["status"] == "ok"


# ──────────────────────────────────────────────
# 结果结构
# ──────────────────────────────────────────────

class TestResultStructure:
    def test_result_has_all_fields(self, monkeypatch):
        monkeypatch.setattr(rec, "_collect_qdrant_ids", lambda: (set(), set()))
        monkeypatch.setattr(rec, "_collect_fts5_ids", lambda: set())
        monkeypatch.setattr(rec, "_collect_salience_ids", lambda: set())
        monkeypatch.setattr(rec, "_load_config", lambda: {})
        monkeypatch.setattr(rec, "_fetch_qdrant_content", lambda x: {})

        result = rec.reconcile_all()

        assert "timestamp" in result
        assert "elapsed_ms" in result
        assert "counts" in result
        assert "orphan_cleaned" in result
        assert "warnings" in result
        assert "status" in result
        assert "fts5_backfilled" in result
        assert result["elapsed_ms"] >= 0

    def test_result_stored_globally(self, monkeypatch):
        monkeypatch.setattr(rec, "_collect_qdrant_ids", lambda: (set(), set()))
        monkeypatch.setattr(rec, "_collect_fts5_ids", lambda: set())
        monkeypatch.setattr(rec, "_collect_salience_ids", lambda: set())
        monkeypatch.setattr(rec, "_load_config", lambda: {})
        monkeypatch.setattr(rec, "_fetch_qdrant_content", lambda x: {})

        result = rec.reconcile_all()
        assert rec._last_result is result
        assert rec._last_run is not None


# ──────────────────────────────────────────────
# get_stats
# ──────────────────────────────────────────────

class TestGetStats:
    def test_get_stats_includes_last_result(self, monkeypatch):
        monkeypatch.setattr(rec, "_collect_qdrant_ids", lambda: ({"a1"}, set()))
        monkeypatch.setattr(rec, "_collect_fts5_ids", lambda: {"a1"})
        monkeypatch.setattr(rec, "_collect_salience_ids", lambda: {"a1"})

        stats = rec.get_stats()
        assert stats["qdrant_active"] == 1
        assert stats["fts5"] == 1
        assert stats["salience"] == 1
        assert stats["last_reconcile_at"] is None  # hasn't run yet
        assert stats["last_reconcile_result"] is None


# ──────────────────────────────────────────────
# start / stop / is_running
# ──────────────────────────────────────────────

class TestStartStop:
    def test_is_running_default(self):
        assert rec.is_running() is False

    def test_stop_sets_flag(self):
        rec._running = True
        rec.stop()
        assert rec._running is False

    def test_double_start_warns(self, monkeypatch):
        rec._running = True
        # start_reconcile_thread should warn and not create a new thread
        import logging
        with mock.patch.object(logging.getLogger("mem0x.reconcile"), "warning") as mock_warn:
            rec.start_reconcile_thread()
            mock_warn.assert_called()
        rec._running = False


# ──────────────────────────────────────────────
# _load_config
# ──────────────────────────────────────────────

class TestLoadConfig:
    def test_reads_from_env(self, monkeypatch, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"reconcile": {"auto_backfill_fts5": true}}')
        monkeypatch.setenv("MEM0X_CONFIG", str(config_file))
        cfg = rec._load_config()
        assert cfg.get("auto_backfill_fts5") is True

    def test_missing_file_returns_empty(self, monkeypatch):
        monkeypatch.setenv("MEM0X_CONFIG", "/nonexistent/path.json")
        cfg = rec._load_config()
        assert cfg == {}
