"""tags_hook 单元测试 — 标签提取 hook。"""
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestTagsHookOnAdd:
    def test_on_add_basic(self):
        """基本 on_add。"""
        from wrapper.tags_hook import on_add

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.collection_name = "test"

        with mock.patch("wrapper.spacy_ner.extract_tags", return_value=["tag1", "tag2"]):
            on_add(mock_mem, "mem1", "hello world")

        mock_mem.vector_store.client.set_payload.assert_called_once()

    def test_on_add_no_tags(self):
        """无 tags 时清空。"""
        from wrapper.tags_hook import on_add

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.collection_name = "test"

        with mock.patch("wrapper.spacy_ner.extract_tags", return_value=[]):
            on_add(mock_mem, "mem1", "hello world")

        # Should set empty tags
        mock_mem.vector_store.client.set_payload.assert_called_once()


class TestTagsHookOnUpdate:
    def test_on_update(self):
        """on_update 重新提取 tags。"""
        from wrapper.tags_hook import on_update

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.collection_name = "test"

        with mock.patch("wrapper.spacy_ner.extract_tags", return_value=["new_tag"]):
            on_update(mock_mem, "mem1", "updated content")

        mock_mem.vector_store.client.set_payload.assert_called_once()


class TestTagsHookOnDelete:
    def test_on_delete(self):
        """on_delete 清空 tags。"""
        from wrapper.tags_hook import on_delete

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.collection_name = "test"

        on_delete(mock_mem, "mem1")
        mock_mem.vector_store.client.set_payload.assert_called_once()

    def test_on_delete_exception_safe(self):
        """on_delete 异常安全。"""
        from wrapper.tags_hook import on_delete

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.collection_name = "test"
        mock_mem.vector_store.client.set_payload.side_effect = Exception("error")

        # Should not raise
        on_delete(mock_mem, "mem1")


class TestTagsHookExceptionSafe:
    def test_on_add_exception_safe(self):
        """on_add 异常安全。"""
        from wrapper.tags_hook import on_add

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.vector_store.client.set_payload.side_effect = Exception("error")

        # Should not raise
        on_add(mock_mem, "mem1", "content")

    def test_on_update_exception_safe(self):
        """on_update 异常安全。"""
        from wrapper.tags_hook import on_update

        mock_mem = mock.MagicMock()
        mock_mem.vector_store.client = mock.MagicMock()
        mock_mem.vector_store.client.set_payload.side_effect = Exception("error")

        # Should not raise
        on_update(mock_mem, "mem1", "content")
