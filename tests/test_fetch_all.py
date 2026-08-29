"""fetch_all 单元测试 — 过滤器构建、Point 转换、分批获取。"""
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestBuildFilter:
    def test_build_filter_none(self):
        """None 过滤器。"""
        from wrapper.fetch_all import _build_filter
        assert _build_filter(None) is None

    def test_build_filter_empty(self):
        """空 dict 过滤器。"""
        from wrapper.fetch_all import _build_filter
        assert _build_filter({}) is None

    def test_build_filter_single(self):
        """单条件过滤器。"""
        from wrapper.fetch_all import _build_filter
        result = _build_filter({"user_id": "test_user"})
        assert result is not None

    def test_build_filter_multiple(self):
        """多条件过滤器。"""
        from wrapper.fetch_all import _build_filter
        result = _build_filter({"user_id": "test", "agent_id": "hermes"})
        assert result is not None

    def test_build_filter_empty_value(self):
        """空值过滤器应跳过。"""
        from wrapper.fetch_all import _build_filter
        result = _build_filter({"user_id": ""})
        assert result is None


class TestPointToItem:
    def test_point_to_item_basic(self):
        """基本 Point 转换。"""
        from wrapper.fetch_all import _point_to_item

        mock_point = mock.MagicMock()
        mock_point.id = "mem1"
        mock_point.payload = {
            "data": "hello world",
            "user_id": "test_user",
            "hash": "abc123",
            "text_lemmatized": "lemma",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }

        result = _point_to_item(mock_point)
        assert result["id"] == "mem1"
        assert result["memory"] == "hello world"
        assert result["metadata"]["user_id"] == "test_user"
        assert "hash" not in result["metadata"]
        assert "text_lemmatized" not in result["metadata"]
        assert "data" not in result["metadata"]

    def test_point_to_item_empty_payload(self):
        """空 payload。"""
        from wrapper.fetch_all import _point_to_item

        mock_point = mock.MagicMock()
        mock_point.id = "mem2"
        mock_point.payload = {}

        result = _point_to_item(mock_point)
        assert result["id"] == "mem2"
        assert result["memory"] == ""


class TestIterBatches:
    @pytest.mark.asyncio
    async def test_iter_batches_basic(self):
        """基本分批获取。"""
        from wrapper.fetch_all import iter_batches

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.vector_store.collection_name = "test_collection"

        mock_pt = mock.MagicMock()
        mock_pt.id = "mem1"
        mock_pt.payload = {"data": "hello"}

        mock_mem.vector_store.client.scroll = mock.MagicMock(return_value=([mock_pt], None))

        batches = []
        async for batch in iter_batches(mock_mem, batch_size=10):
            batches.append(batch)

        assert len(batches) == 1
        assert batches[0][0]["id"] == "mem1"

    @pytest.mark.asyncio
    async def test_iter_batches_empty(self):
        """空结果。"""
        from wrapper.fetch_all import iter_batches

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.vector_store.collection_name = "test_collection"
        mock_mem.vector_store.client.scroll = mock.MagicMock(return_value=([], None))

        batches = []
        async for batch in iter_batches(mock_mem, batch_size=10):
            batches.append(batch)

        assert len(batches) == 0

    @pytest.mark.asyncio
    async def test_iter_batches_max_items(self):
        """限制最大条数。"""
        from wrapper.fetch_all import iter_batches

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.vector_store.collection_name = "test_collection"

        def mock_scroll(**kwargs):
            limit = kwargs.get("limit", 10)
            pts = []
            for i in range(limit):
                pt = mock.MagicMock()
                pt.id = f"mem{i}"
                pt.payload = {"data": f"content {i}"}
                pts.append(pt)
            return (pts, None)

        mock_mem.vector_store.client.scroll = mock_scroll

        batches = []
        async for batch in iter_batches(mock_mem, batch_size=10, max_items=3):
            batches.append(batch)

        total = sum(len(b) for b in batches)
        assert total == 3

    @pytest.mark.asyncio
    async def test_iter_batches_scroll_exception(self):
        """scroll 异常时 break。"""
        from wrapper.fetch_all import iter_batches

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.vector_store.collection_name = "test_collection"
        mock_mem.vector_store.client.scroll = mock.MagicMock(side_effect=Exception("scroll error"))

        batches = []
        async for batch in iter_batches(mock_mem, batch_size=10):
            batches.append(batch)

        assert len(batches) == 0

    @pytest.mark.asyncio
    async def test_iter_batches_pagination(self):
        """分页：多次 scroll 返回不同 offset。"""
        from wrapper.fetch_all import iter_batches

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.vector_store.collection_name = "test_collection"

        call_count = [0]

        def mock_scroll(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                pt = mock.MagicMock()
                pt.id = "mem1"
                pt.payload = {"data": "first"}
                return ([pt], "offset_2")
            else:
                pt = mock.MagicMock()
                pt.id = "mem2"
                pt.payload = {"data": "second"}
                return ([pt], None)

        mock_mem.vector_store.client.scroll = mock_scroll

        batches = []
        async for batch in iter_batches(mock_mem, batch_size=1):
            batches.append(batch)

        assert len(batches) == 2

    @pytest.mark.asyncio
    async def test_iter_batches_next_offset_is_none(self):
        """next_offset 为 None 时停止。"""
        from wrapper.fetch_all import iter_batches

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.vector_store.collection_name = "test_collection"

        pt = mock.MagicMock()
        pt.id = "mem1"
        pt.payload = {"data": "hello"}
        mock_mem.vector_store.client.scroll = mock.MagicMock(return_value=([pt], None))

        batches = []
        async for batch in iter_batches(mock_mem, batch_size=10):
            batches.append(batch)

        assert len(batches) == 1

    @pytest.mark.asyncio
    async def test_iter_batches_next_offset_same_breaks(self):
        """next_offset == offset 时 break。"""
        from wrapper.fetch_all import iter_batches

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.vector_store.collection_name = "test_collection"

        call_count = [0]

        def mock_scroll(**kwargs):
            call_count[0] += 1
            pt = mock.MagicMock()
            pt.id = f"mem{call_count[0]}"
            pt.payload = {"data": f"batch{call_count[0]}"}
            # Return same offset as input (if any) to trigger break
            offset = kwargs.get("offset")
            return ([pt], offset)

        mock_mem.vector_store.client.scroll = mock_scroll

        batches = []
        async for batch in iter_batches(mock_mem, batch_size=10):
            batches.append(batch)

        # First: offset=None, next_offset=None -> break after 1st batch
        assert len(batches) == 1


class TestGetDistinctUserIds:
    @pytest.mark.asyncio
    async def test_get_distinct_user_ids_facet(self):
        """使用 facet API。"""
        from wrapper.fetch_all import get_distinct_user_ids

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.vector_store.collection_name = "test_collection"

        mock_hit1 = mock.MagicMock()
        mock_hit1.value = "user1"
        mock_hit2 = mock.MagicMock()
        mock_hit2.value = "user2"
        mock_mem.vector_store.client.facet = mock.MagicMock(
            return_value=mock.MagicMock(hits=[mock_hit1, mock_hit2])
        )

        result = await get_distinct_user_ids(mock_mem)
        assert "user1" in result
        assert "user2" in result

    @pytest.mark.asyncio
    async def test_get_distinct_user_ids_fallback_scroll(self):
        """facet 失败时 fallback 到 scroll。"""
        from wrapper.fetch_all import get_distinct_user_ids

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.vector_store.collection_name = "test_collection"

        mock_mem.vector_store.client.facet = mock.MagicMock(side_effect=Exception("not supported"))

        mock_pt1 = mock.MagicMock()
        mock_pt1.payload = {"user_id": "user1"}
        mock_pt2 = mock.MagicMock()
        mock_pt2.payload = {"user_id": "user2"}
        mock_mem.vector_store.client.scroll = mock.MagicMock(
            return_value=([mock_pt1, mock_pt2], None)
        )

        result = await get_distinct_user_ids(mock_mem)
        assert "user1" in result
        assert "user2" in result


class TestFetchAllMemories:
    @pytest.mark.asyncio
    async def test_fetch_all_memories(self):
        """fetch_all_memories。"""
        from wrapper.fetch_all import fetch_all_memories

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.vector_store.collection_name = "test_collection"

        mock_pt = mock.MagicMock()
        mock_pt.id = "mem1"
        mock_pt.payload = {"data": "hello"}

        mock_mem.vector_store.client.scroll = mock.MagicMock(return_value=([mock_pt], None))

        result = await fetch_all_memories(mock_mem)
        assert len(result) == 1
