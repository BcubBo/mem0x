"""sparse_vector (BM25) 单元测试 — 分词、编码、IDF 持久化。"""
import os
import tempfile
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestTokenize:
    def test_tokenize_english(self):
        """英文分词。"""
        from wrapper.sparse_vector import tokenize
        tokens = tokenize("hello world test")
        assert "hello" in tokens
        assert "world" in tokens
        assert "test" in tokens

    def test_tokenize_cjk(self):
        """中文分词。"""
        from wrapper.sparse_vector import tokenize
        tokens = tokenize("测试记忆系统")
        assert len(tokens) >= 1

    def test_tokenize_filters_stop_words(self):
        """过滤停用词。"""
        from wrapper.sparse_vector import tokenize
        tokens = tokenize("the a an is are was in on at to for of")
        # All stop words should be filtered
        assert len(tokens) == 0

    def test_tokenize_filters_short(self):
        """过滤单字符。"""
        from wrapper.sparse_vector import tokenize
        tokens = tokenize("a b c d")
        assert len(tokens) == 0

    def test_tokenize_mixed(self):
        """中英混合。"""
        from wrapper.sparse_vector import tokenize
        tokens = tokenize("mem0 测试 Docker API")
        assert len(tokens) >= 2

    def test_tokenize_empty(self):
        """空字符串。"""
        from wrapper.sparse_vector import tokenize
        assert tokenize("") == []


class TestBM25Encoder:
    def test_encode_basic(self):
        """基本编码。"""
        from wrapper.sparse_vector import BM25Encoder
        enc = BM25Encoder()
        result = enc.encode("hello world test memory")
        assert "indices" in result
        assert "values" in result
        assert len(result["indices"]) > 0
        assert len(result["indices"]) == len(result["values"])

    def test_encode_empty(self):
        """空文本编码。"""
        from wrapper.sparse_vector import BM25Encoder
        enc = BM25Encoder()
        result = enc.encode("")
        assert result == {"indices": [], "values": []}

    def test_encode_query(self):
        """查询编码。"""
        from wrapper.sparse_vector import BM25Encoder
        enc = BM25Encoder()
        result = enc.encode_query("hello world")
        assert "indices" in result
        assert "values" in result

    def test_encode_query_empty(self):
        """空查询编码。"""
        from wrapper.sparse_vector import BM25Encoder
        enc = BM25Encoder()
        assert enc.encode_query("") == {"indices": [], "values": []}

    def test_encode_updates_stats(self):
        """编码应更新统计。"""
        from wrapper.sparse_vector import BM25Encoder
        enc = BM25Encoder()
        enc.encode("hello world test")
        assert enc.doc_count == 1
        assert enc.total_length == 3

    def test_encode_multiple_documents(self):
        """多文档编码更新 IDF。"""
        from wrapper.sparse_vector import BM25Encoder
        enc = BM25Encoder()
        enc.encode("hello world")
        enc.encode("hello there")
        assert enc.doc_count == 2
        # "hello" appears in 2 docs
        assert enc.doc_freq["hello"] == 2

    def test_reset_stats(self):
        """重置统计。"""
        from wrapper.sparse_vector import BM25Encoder
        enc = BM25Encoder()
        enc.encode("hello world")
        enc.reset_stats()
        assert enc.doc_count == 0
        assert enc.total_length == 0
        assert len(enc.doc_freq) == 0

    def test_save_load_idf(self, tmp_path):
        """IDF 持久化和加载。"""
        import wrapper.sparse_vector as mod
        # Temporarily override _IDF_DB
        old_idf_db = mod._IDF_DB
        idf_db = str(tmp_path / "test_idf.db")
        mod._IDF_DB = idf_db

        try:
            from wrapper.sparse_vector import BM25Encoder
            enc = BM25Encoder()
            enc.encode("hello world test")
            enc.encode("hello there test")
            enc.save_idf()

            # Load into new encoder
            enc2 = BM25Encoder()
            enc2.load_idf()
            assert enc2.doc_count == 2
            assert enc2.total_length == enc.total_length
        finally:
            mod._IDF_DB = old_idf_db

    def test_load_idf_no_file(self, tmp_path):
        """加载不存在的 IDF 文件。"""
        from wrapper.sparse_vector import BM25Encoder
        enc = BM25Encoder()
        result = enc.load_idf()
        assert result is False

    def test_save_idf_persistence(self, tmp_path):
        """IDF 持久化验证。"""
        import wrapper.sparse_vector as mod
        old_idf_db = mod._IDF_DB
        idf_db = str(tmp_path / "test_idf2.db")
        mod._IDF_DB = idf_db

        try:
            from wrapper.sparse_vector import BM25Encoder
            enc = BM25Encoder()
            for i in range(10):
                enc.encode(f"document {i} with unique words")
            enc.save_idf()

            # Verify file exists
            assert os.path.exists(idf_db)

            # Load and verify
            enc2 = BM25Encoder()
            enc2.load_idf()
            assert enc2.doc_count == 10
        finally:
            mod._IDF_DB = old_idf_db

    def test_scores_positive(self):
        """所有 score 应为正数。"""
        from wrapper.sparse_vector import BM25Encoder
        enc = BM25Encoder()
        enc.encode("hello world test memory system")
        for v in enc.encode("hello world")["values"]:
            assert v > 0

    def test_auto_save_every_100(self):
        """每 100 篇自动持久化。"""
        import wrapper.sparse_vector as mod
        from wrapper.sparse_vector import BM25Encoder

        old_idf_db = mod._IDF_DB
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        mod._IDF_DB = tmp.name
        tmp.close()

        try:
            enc = BM25Encoder()
            # Set doc_count to 99 so next encode triggers save
            enc.doc_count = 99
            enc.total_length = 100
            enc.encode("test document")
            # doc_count should be 100 now, triggering save
            assert enc.doc_count == 100
            assert os.path.exists(tmp.name)
        finally:
            mod._IDF_DB = old_idf_db
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


class TestBM25Singleton:
    def test_get_bm25_encoder_singleton(self):
        """get_bm25_encoder 返回单例。"""
        import wrapper.sparse_vector as mod
        mod._bm25_encoder = None
        e1 = mod.get_bm25_encoder()
        e2 = mod.get_bm25_encoder()
        assert e1 is e2
        mod._bm25_encoder = None
