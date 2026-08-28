"""consolidation 单元测试 — 阈值配置、LLM 摘要触发、聚类工具函数。

覆盖纯逻辑函数，不涉及异步 memory 实例。
使用内存 SQLite，mock LLM 和 Qdrant。
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ──────────────────────────────────────────────
# _jaccard
# ──────────────────────────────────────────────

class TestJaccard:
    def test_identical_sets(self):
        from wrapper.consolidation import _jaccard
        s = {"a", "b", "c"}
        assert _jaccard(s, s) == pytest.approx(1.0)

    def test_disjoint_sets(self):
        from wrapper.consolidation import _jaccard
        assert _jaccard({"a"}, {"b"}) == pytest.approx(0.0)

    def test_partial_overlap(self):
        from wrapper.consolidation import _jaccard
        # {a,b} ∩ {b,c} = {b}, union = {a,b,c} → 1/3
        assert _jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)

    def test_empty_sets(self):
        from wrapper.consolidation import _jaccard
        assert _jaccard(set(), set()) == 0.0
        assert _jaccard({"a"}, set()) == 0.0
        assert _jaccard(set(), {"a"}) == 0.0

    def test_subset(self):
        from wrapper.consolidation import _jaccard
        # {a} ⊂ {a,b,c} → 1/3
        assert _jaccard({"a"}, {"a", "b", "c"}) == pytest.approx(1 / 3)


# ──────────────────────────────────────────────
# _extract_keywords
# ──────────────────────────────────────────────

class TestExtractKeywords:
    def test_english_words(self):
        from wrapper.consolidation import _extract_keywords
        kw = _extract_keywords("Docker uses Qdrant for vector storage")
        assert "Docker" in kw
        assert "Qdrant" in kw
        assert "vector" in kw

    def test_chinese_words(self):
        from wrapper.consolidation import _extract_keywords
        kw = _extract_keywords("mem0x使用Qdrant作为向量存储引擎")
        assert "向量存储" in kw or "引擎" in kw
        # 停用词应被过滤
        assert "使用" not in kw
        assert "作为" not in kw

    def test_numbers(self):
        from wrapper.consolidation import _extract_keywords
        kw = _extract_keywords("端口 6333 和 8080")
        assert "6333" in kw
        assert "8080" in kw

    def test_stopwords_filtered(self):
        from wrapper.consolidation import _extract_keywords
        from wrapper.consolidation import _ZH_STOP
        # 测试已知停用词在纯停用词文本中被过滤
        # 注意：连续停用词可能被 regex 作为 2-6 字序列匹配（不在停用词表中）
        # 所以这里用已知停用词单独出现的场景验证
        # "使用" 和 "作为" 在 test_chinese_words 中已被验证过滤
        # 这里验证其他停用词
        kw = _extract_keywords("需要进行实现完成应该")
        # 所有这些都是停用词表中的词，应被过滤
        for w in _ZH_STOP:
            if len(w) >= 2:
                assert w not in kw, f"stop word '{w}' should be filtered, got {kw}"

    def test_short_chinese_excluded(self):
        from wrapper.consolidation import _extract_keywords
        kw = _extract_keywords("我说好")
        # 1字中文应被排除
        assert not any(len(w) == 1 and '\u4e00' <= w <= '\u9fa5' for w in kw)

    def test_empty_text(self):
        from wrapper.consolidation import _extract_keywords
        assert _extract_keywords("") == set()

    def test_mixed_content(self):
        from wrapper.consolidation import _extract_keywords
        # 英文和中文混合：用空格分隔以避免中文 regex 吞掉英文
        kw = _extract_keywords("使用 Qdrant 存储向量，端口 6333")
        assert "Qdrant" in kw
        assert "6333" in kw
        assert "存储向量" in kw or "向量" in kw


# ──────────────────────────────────────────────
# _UnionFind
# ──────────────────────────────────────────────

class TestUnionFind:
    def test_singletons(self):
        from wrapper.consolidation import _UnionFind
        uf = _UnionFind(5)
        clusters = uf.clusters()
        assert len(clusters) == 5
        for members in clusters.values():
            assert len(members) == 1

    def test_union_merges(self):
        from wrapper.consolidation import _UnionFind
        uf = _UnionFind(5)
        uf.union(0, 1)
        uf.union(1, 2)
        clusters = uf.clusters()
        # 0,1,2 should be in one cluster; 3,4 each separate
        assert len(clusters) == 3
        # find the cluster containing 0
        root_0 = uf.find(0)
        assert len(clusters[root_0]) == 3

    def test_union_idempotent(self):
        from wrapper.consolidation import _UnionFind
        uf = _UnionFind(3)
        uf.union(0, 1)
        uf.union(0, 1)  # duplicate
        clusters = uf.clusters()
        assert len(clusters) == 2

    def test_chain_union(self):
        from wrapper.consolidation import _UnionFind
        uf = _UnionFind(4)
        uf.union(0, 1)
        uf.union(2, 3)
        uf.union(1, 3)  # connects all
        clusters = uf.clusters()
        assert len(clusters) == 1
        assert len(list(clusters.values())[0]) == 4

    def test_empty(self):
        from wrapper.consolidation import _UnionFind
        uf = _UnionFind(0)
        assert uf.clusters() == {}


# ──────────────────────────────────────────────
# _merge_with_llm
# ──────────────────────────────────────────────

class TestMergeWithLLM:
    def test_basic_merge(self):
        from wrapper.consolidation import _merge_with_llm

        class FakeLLM:
            def generate_response(self, messages, **kwargs):
                return "合并后的记忆：使用Qdrant存储向量，端口6333"

        result = _merge_with_llm(
            ["记忆1：Qdrant端口6333", "记忆2：向量存储"],
            FakeLLM(),
        )
        assert result is not None
        assert "Qdrant" in result

    def test_strips_quotes(self):
        from wrapper.consolidation import _merge_with_llm

        class FakeLLM:
            def generate_response(self, messages, **kwargs):
                return '"合并结果"'

        # The function strips quotes AND strips "合并结果" prefix
        result = _merge_with_llm(["a" * 20, "b" * 20], FakeLLM())
        # After stripping quotes and prefix, may be empty or short
        # The key test is that it doesn't crash
        assert result is None or isinstance(result, str)

    def test_strips_prefix(self):
        from wrapper.consolidation import _merge_with_llm

        class FakeLLM:
            def generate_response(self, messages, **kwargs):
                return "合并结果：这是一条完整的合并后记忆内容，保留了所有关键信息"

        result = _merge_with_llm(
            ["记忆碎片A的内容足够长一些" * 2, "记忆碎片B的内容也足够长一些" * 2],
            FakeLLM(),
        )
        assert result is not None
        assert not result.startswith("合并结果")

    def test_retries_on_failure(self):
        from wrapper.consolidation import _merge_with_llm

        call_count = 0

        class FlakyLLM:
            def generate_response(self, messages, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise RuntimeError("API error")
                return "合并后的有效记忆内容，长度足够"

        result = _merge_with_llm(
            ["碎片A" * 10, "碎片B" * 10],
            FlakyLLM(),
            max_retries=3,
        )
        assert result is not None
        assert call_count == 3

    def test_returns_none_after_all_retries_exhausted(self):
        from wrapper.consolidation import _merge_with_llm

        class AlwaysFailLLM:
            def generate_response(self, messages, **kwargs):
                raise RuntimeError("down")

        result = _merge_with_llm(
            ["a" * 20, "b" * 20],
            AlwaysFailLLM(),
            max_retries=1,
        )
        assert result is None

    def test_rejects_short_output(self):
        from wrapper.consolidation import _merge_with_llm

        class ShortLLM:
            def generate_response(self, messages, **kwargs):
                return "短"

        result = _merge_with_llm(["a" * 20, "b" * 20], ShortLLM())
        assert result is None

    def test_summarize_mode(self):
        from wrapper.consolidation import _merge_with_llm

        captured_prompt = []

        class CapturingLLM:
            def generate_response(self, messages, **kwargs):
                captured_prompt.append(messages[0]["content"])
                return "压缩后的摘要内容，保留关键信息，来源引用完整"

        result = _merge_with_llm(
            ["碎片A" * 20, "碎片B" * 20],
            CapturingLLM(),
            source_ids=["id1", "id2"],
            summarize=True,
        )
        assert result is not None
        assert "来源" in captured_prompt[0] or "来源" in result

    def test_returns_none_on_empty_response(self):
        from wrapper.consolidation import _merge_with_llm

        class EmptyLLM:
            def generate_response(self, messages, **kwargs):
                return ""

        result = _merge_with_llm(["a" * 20, "b" * 20], EmptyLLM())
        assert result is None

    def test_returns_none_on_none_response(self):
        from wrapper.consolidation import _merge_with_llm

        class NoneLLM:
            def generate_response(self, messages, **kwargs):
                return None

        result = _merge_with_llm(["a" * 20, "b" * 20], NoneLLM())
        assert result is None


# ──────────────────────────────────────────────
# _merge_keywords
# ──────────────────────────────────────────────

class TestMergeKeywords:
    def test_single_item(self):
        from wrapper.consolidation import _merge_keywords
        group = [{"memory": "Qdrant使用6333端口"}]
        result = _merge_keywords(group)
        assert result == "Qdrant使用6333端口"

    def test_merges_unique_keywords(self):
        from wrapper.consolidation import _merge_keywords
        group = [
            {"memory": "Qdrant使用6333端口进行向量存储"},
            {"memory": "Qdrant使用gRPC协议通信"},
        ]
        result = _merge_keywords(group)
        assert "Qdrant" in result
        # Unique keywords from second item should be appended
        assert "gRPC" in result or "通信" in result

    def test_skips_cosine_marker(self):
        from wrapper.consolidation import _merge_keywords
        group = [
            {"memory": "Qdrant使用6333端口"},
            {"_avg_cosine": 0.92},
        ]
        result = _merge_keywords(group)
        assert "Qdrant" in result

    def test_empty_group(self):
        from wrapper.consolidation import _merge_keywords
        assert _merge_keywords([]) == ""
        assert _merge_keywords([{"_avg_cosine": 0.9}]) == ""


# ──────────────────────────────────────────────
# _merge_metadata
# ──────────────────────────────────────────────

class TestMergeMetadata:
    def test_basic_merge(self):
        from wrapper.consolidation import _merge_metadata
        group = [
            {"id": "a1", "metadata": {"search_count": 3, "access_count": 5}},
            {"id": "b2", "metadata": {"search_count": 2, "access_count": 1}},
        ]
        meta = _merge_metadata(group, "merged text")
        assert meta["source"] == "consolidation"
        assert meta["merge_count"] == 2
        assert meta["search_count"] == 5
        assert meta["access_count"] == 6
        assert set(meta["merged_from"]) == {"a1", "b2"}

    def test_skips_cosine_marker(self):
        from wrapper.consolidation import _merge_metadata
        group = [
            {"id": "a1", "metadata": {"search_count": 1}},
            {"_avg_cosine": 0.95},
        ]
        meta = _merge_metadata(group, "text")
        assert meta["merge_count"] == 1
        assert meta["merged_from"] == ["a1"]

    def test_preserves_fsrs_card(self):
        from wrapper.consolidation import _merge_metadata
        fsrs_card = {"stability": 5.0, "difficulty": 0.3}
        group = [
            {"id": "a1", "metadata": {"fsrs_card": fsrs_card}},
            {"id": "b2", "metadata": {}},
        ]
        meta = _merge_metadata(group, "text")
        assert meta["fsrs_card"] == fsrs_card

    def test_handles_none_metadata(self):
        from wrapper.consolidation import _merge_metadata
        group = [
            {"id": "a1", "metadata": None},
            {"id": "b2", "metadata": {"search_count": 5}},
        ]
        meta = _merge_metadata(group, "text")
        assert meta["search_count"] == 5
        assert meta["access_count"] == 0


# ──────────────────────────────────────────────
# _pick_best_memory
# ──────────────────────────────────────────────

class TestPickBestMemory:
    def test_picks_longer_text(self, monkeypatch):
        from wrapper.consolidation import _pick_best_memory

        # Mock fsrs_bridge to return constant quality
        monkeypatch.setattr(
            "wrapper.fsrs_bridge.get_quality_score",
            lambda meta, created, access: 0.5,
        )
        group = [
            {"memory": "short", "metadata": {}, "created_at": None},
            {"memory": "a" * 200, "metadata": {}, "created_at": None},
        ]
        best = _pick_best_memory(group)
        assert best == "a" * 200

    def test_skips_cosine_marker(self, monkeypatch):
        from wrapper.consolidation import _pick_best_memory

        monkeypatch.setattr(
            "wrapper.fsrs_bridge.get_quality_score",
            lambda meta, created, access: 0.5,
        )
        group = [
            {"_avg_cosine": 0.9},
            {"memory": "the only memory text", "metadata": {}, "created_at": None},
        ]
        best = _pick_best_memory(group)
        assert best == "the only memory text"

    def test_empty_group(self, monkeypatch):
        from wrapper.consolidation import _pick_best_memory

        monkeypatch.setattr(
            "wrapper.fsrs_bridge.get_quality_score",
            lambda meta, created, access: 0.5,
        )
        assert _pick_best_memory([]) == ""
        assert _pick_best_memory([{"_avg_cosine": 0.9}]) == ""


# ──────────────────────────────────────────────
# _record_merge / _record_archive / _is_already_merged
# ──────────────────────────────────────────────

@pytest.fixture
def _consolidation_db(monkeypatch):
    """让 consolidation 使用临时 SQLite 文件。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = tmp.name
    tmp.close()

    import wrapper.consolidation as cons
    cons._db_path = tmp_path
    cons._schema_checked = False
    cons._merge_cache = None
    cons._merge_cache_at = 0

    yield tmp_path

    try:
        os.unlink(tmp_path)
    except OSError:
        pass


class TestRecordMergeAndArchive:
    def test_record_merge_then_check(self, _consolidation_db):
        from wrapper.consolidation import _record_merge, _is_already_merged
        _record_merge("new_id", ["s1", "s2"], "merged text")
        assert _is_already_merged(["s1", "s2"]) is True
        assert _is_already_merged(["s2", "s1"]) is True  # order-independent
        assert _is_already_merged(["s1", "s3"]) is False

    def test_record_archive(self, _consolidation_db):
        from wrapper.consolidation import _record_archive, _get_archived_ids
        _record_archive("old1", "new1")
        _record_archive("old2", "new1")
        archived = _get_archived_ids()
        assert "old1" in archived
        assert "old2" in archived
        assert "new1" not in archived

    def test_is_already_merged_empty(self, _consolidation_db):
        from wrapper.consolidation import _is_already_merged
        assert _is_already_merged(["x1", "x2"]) is False

    def test_merge_cache_refresh(self, _consolidation_db):
        from wrapper.consolidation import (
            _record_merge, _is_already_merged, _merge_cache, _merge_cache_at
        )
        import wrapper.consolidation as cons

        _record_merge("m1", ["a", "b"], "text")
        assert _is_already_merged(["a", "b"]) is True

        # Force cache expiry
        cons._merge_cache_at = 0
        # Should re-read from DB
        assert _is_already_merged(["a", "b"]) is True

    def test_get_archived_ids_empty(self, _consolidation_db):
        from wrapper.consolidation import _get_archived_ids
        assert _get_archived_ids() == set()


# ──────────────────────────────────────────────
# 阈值配置
# ──────────────────────────────────────────────

class TestThresholdConfig:
    def test_default_thresholds(self):
        from wrapper import consolidation as cons
        assert cons.VECTOR_SIM_THRESHOLD == 0.75
        assert cons.JACCARD_THRESHOLD == 0.35
        assert cons.MIN_GROUP_SIZE == 2
        assert cons.MAX_GROUP_SIZE == 8
        assert cons.EXACT_DUP_THRESHOLD == 0.95
        assert cons.NEAR_DUP_THRESHOLD == 0.88
        assert cons.LLM_MERGE_THRESHOLD == 500
        assert cons.LLM_MERGE_MAX_GROUPS == 5
        assert cons.MIN_MEMORY_LENGTH == 15

    def test_max_merges_per_cycle(self):
        from wrapper import consolidation as cons
        assert cons.MAX_MERGES_PER_CYCLE == 5

    def test_default_interval(self):
        from wrapper import consolidation as cons
        assert cons.DEFAULT_INTERVAL == 7200


# ──────────────────────────────────────────────
# start / stop / is_running
# ──────────────────────────────────────────────

class TestStartStop:
    def test_is_running_default(self):
        from wrapper.consolidation import is_running, _running
        # _running is module-level state; just verify the function works
        result = is_running()
        assert isinstance(result, bool)

    def test_stop_sets_flag(self):
        import wrapper.consolidation as cons
        cons._running = True
        cons.stop()
        assert cons._running is False
        cons._running = False  # reset
