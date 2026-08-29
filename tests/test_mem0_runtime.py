"""mem0_runtime 单元测试 — 配置加载、rerank。"""
import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def clean_config(monkeypatch):
    """重置配置缓存。"""
    import wrapper.mem0_runtime as rt
    rt._config_cache = None
    rt._rerank_config_cache = None
    rt._memory_instance = None
    yield
    rt._config_cache = None
    rt._rerank_config_cache = None
    rt._memory_instance = None


class TestLoadConfig:
    def test_load_config_from_file(self, tmp_path, clean_config, monkeypatch):
        """从文件加载配置。"""
        import wrapper.mem0_runtime as rt
        config = {"mem0": {"config": "test"}, "rerank": {}}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config))

        monkeypatch.setattr(rt, "MEM0X_HOME", tmp_path)
        monkeypatch.setattr(rt, "PROJECT_DIR", tmp_path)
        monkeypatch.delenv("MEM0X_CONFIG", raising=False)

        rt._config_cache = None
        result = rt.load_config(str(config_file))
        assert result["mem0"]["config"] == "test"

    def test_load_config_from_env(self, tmp_path, clean_config, monkeypatch):
        """从环境变量加载配置。"""
        import wrapper.mem0_runtime as rt
        config = {"mem0": {}}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config))

        monkeypatch.setenv("MEM0X_CONFIG", str(config_file))
        monkeypatch.setattr(rt, "MEM0X_HOME", tmp_path)
        monkeypatch.setattr(rt, "PROJECT_DIR", tmp_path)

        rt._config_cache = None
        result = rt.load_config()
        assert "mem0" in result
        monkeypatch.delenv("MEM0X_CONFIG", raising=False)

    def test_load_config_not_found(self, clean_config, monkeypatch, tmp_path):
        """配置文件不存在。"""
        import wrapper.mem0_runtime as rt
        rt._config_cache = None

        nonexistent = tmp_path / "nonexistent_config"
        monkeypatch.setattr(rt, "MEM0X_HOME", nonexistent)
        monkeypatch.setattr(rt, "PROJECT_DIR", nonexistent)
        monkeypatch.delenv("MEM0X_CONFIG", raising=False)

        with pytest.raises(FileNotFoundError):
            rt.load_config()

    def test_load_config_caches(self, tmp_path, clean_config, monkeypatch):
        """配置缓存。"""
        import wrapper.mem0_runtime as rt
        config = {"mem0": {"cached": True}}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config))

        rt._config_cache = None
        monkeypatch.setattr(rt, "MEM0X_HOME", tmp_path)
        monkeypatch.setattr(rt, "PROJECT_DIR", tmp_path)

        result1 = rt.load_config(str(config_file))
        assert result1["mem0"]["cached"] is True

        result2 = rt.load_config(str(config_file))
        assert result1 is result2


class TestOverrideFromEnv:
    def test_override_llm_key(self, clean_config, monkeypatch):
        """环境变量覆盖 LLM key。"""
        import wrapper.mem0_runtime as rt
        cfg = {"mem0": {"llm": {"config": {}}}}
        monkeypatch.setenv("BO_MEM0_LLM_API_KEY", "test_key")
        rt._override_from_env(cfg)
        assert cfg["mem0"]["llm"]["config"]["api_key"] == "test_key"
        monkeypatch.delenv("BO_MEM0_LLM_API_KEY", raising=False)

    def test_override_embedder_key(self, clean_config, monkeypatch):
        """环境变量覆盖 embedder key。"""
        import wrapper.mem0_runtime as rt
        cfg = {"mem0": {"embedder": {"config": {}}}}
        monkeypatch.setenv("BO_MEM0_EMBEDDER_API_KEY", "test_embed")
        rt._override_from_env(cfg)
        assert cfg["mem0"]["embedder"]["config"]["api_key"] == "test_embed"
        monkeypatch.delenv("BO_MEM0_EMBEDDER_API_KEY", raising=False)

    def test_override_qdrant_key(self, clean_config, monkeypatch):
        """环境变量覆盖 qdrant key。"""
        import wrapper.mem0_runtime as rt
        cfg = {"mem0": {"vector_store": {"config": {}}}}
        monkeypatch.setenv("BO_MEM0_QDRANT_API_KEY", "test_qdrant")
        rt._override_from_env(cfg)
        assert cfg["mem0"]["vector_store"]["config"]["api_key"] == "test_qdrant"
        monkeypatch.delenv("BO_MEM0_QDRANT_API_KEY", raising=False)

    def test_override_rerank_key(self, clean_config, monkeypatch):
        """环境变量覆盖 rerank key。"""
        import wrapper.mem0_runtime as rt
        cfg = {"rerank": {"config": {}}}
        monkeypatch.setenv("BO_MEM0_RERANK_API_KEY", "test_rerank")
        rt._override_from_env(cfg)
        assert cfg["rerank"]["config"]["api_key"] == "test_rerank"
        monkeypatch.delenv("BO_MEM0_RERANK_API_KEY", raising=False)

    def test_no_env_var(self, clean_config):
        """无环境变量不修改。"""
        import wrapper.mem0_runtime as rt
        cfg = {"mem0": {"llm": {"config": {}}}}
        rt._override_from_env(cfg)
        assert cfg["mem0"]["llm"]["config"] == {}


class TestResetConfigCache:
    def test_reset(self, clean_config):
        """重置配置缓存。"""
        import wrapper.mem0_runtime as rt
        rt._config_cache = {"test": True}
        rt._rerank_config_cache = {"test": True}
        rt.reset_config_cache()
        assert rt._config_cache is None
        assert rt._rerank_config_cache is None


class TestResetMemorySingleton:
    def test_reset(self, clean_config):
        """重置 mem0 单例。"""
        import wrapper.mem0_runtime as rt
        rt._memory_instance = mock.MagicMock()
        rt.reset_memory_singleton()
        assert rt._memory_instance is None


class TestGetMemorySingleton:
    def test_singleton_same_instance(self, clean_config):
        """单例返回同一实例。"""
        import wrapper.mem0_runtime as rt
        rt._memory_instance = mock.MagicMock()
        m1 = rt.get_memory()
        m2 = rt.get_memory()
        assert m1 is m2

    def test_singleton_with_config(self, clean_config):
        """使用自定义配置。"""
        import wrapper.mem0_runtime as rt
        rt._memory_instance = None
        config = {"mem0": {"config": {}}}

        with mock.patch("mem0.AsyncMemory") as mock_async:
            mock_async.from_config.return_value = mock.MagicMock()
            result = rt.get_memory(config)
            mock_async.from_config.assert_called_once_with(config["mem0"])
            assert result is not None


class TestLoadRerankConfig:
    def test_load_rerank_config(self, clean_config):
        """加载 rerank 配置。"""
        import wrapper.mem0_runtime as rt
        config = {"rerank": {"config": {"api_key": "test_key"}, "provider": "openai"}}
        result = rt._load_rerank_config(config)
        assert result is not None

    def test_no_rerank_config(self, clean_config):
        """无 rerank 配置。"""
        import wrapper.mem0_runtime as rt
        config = {}
        result = rt._load_rerank_config(config)
        assert result is None

    def test_rerank_config_no_key(self, clean_config):
        """rerank 配置无 api_key。"""
        import wrapper.mem0_runtime as rt
        config = {"rerank": {"config": {}}}
        result = rt._load_rerank_config(config)
        assert result is None


class TestRerank:
    def test_rerank_no_config(self, clean_config):
        """无配置返回空。"""
        import wrapper.mem0_runtime as rt
        result = rt.rerank("query", ["doc1", "doc2"])
        assert result == []

    def test_rerank_unknown_provider(self, clean_config):
        """未知 provider。"""
        import wrapper.mem0_runtime as rt
        config = {"rerank": {"config": {"api_key": "key"}, "provider": "unknown"}}
        result = rt.rerank("query", ["doc1"], config=config)
        assert result == []

    def test_rerank_openai_compatible(self, clean_config):
        """OpenAI 兼容 rerank。"""
        import wrapper.mem0_runtime as rt
        config = {
            "rerank": {
                "config": {
                    "api_key": "test_key",
                    "model": "test_model",
                    "openai_base_url": "https://api.test.com/v1",
                },
                "provider": "siliconflow",
            }
        }
        mock_resp = mock.MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.5},
            ]
        }
        mock_resp.raise_for_status = mock.MagicMock()

        with mock.patch("wrapper.mem0_runtime.req.post", return_value=mock_resp):
            result = rt.rerank("query", ["doc1", "doc2"], config=config)
            assert len(result) == 2
            assert result[0]["relevance_score"] == 0.9

    def test_rerank_jina(self, clean_config):
        """Jina rerank。"""
        import wrapper.mem0_runtime as rt
        config = {
            "rerank": {
                "config": {
                    "api_key": "test_key",
                    "model": "jina-reranker-v2-base-multilingual",
                },
                "provider": "jina",
            }
        }
        mock_resp = mock.MagicMock()
        mock_resp.json.return_value = {"results": [{"index": 0, "relevance_score": 0.8}]}
        mock_resp.raise_for_status = mock.MagicMock()

        with mock.patch("wrapper.mem0_runtime.req.post", return_value=mock_resp):
            result = rt.rerank("query", ["doc1"], config=config)
            assert len(result) == 1

    def test_rerank_cohere(self, clean_config):
        """Cohere rerank。"""
        import wrapper.mem0_runtime as rt
        config = {
            "rerank": {
                "config": {
                    "api_key": "test_key",
                    "model": "rerank-multilingual-v3.0",
                },
                "provider": "cohere",
            }
        }
        mock_resp = mock.MagicMock()
        mock_resp.json.return_value = {"results": [{"index": 0, "relevance_score": 0.7}]}
        mock_resp.raise_for_status = mock.MagicMock()

        with mock.patch("wrapper.mem0_runtime.req.post", return_value=mock_resp):
            result = rt.rerank("query", ["doc1"], config=config)
            assert len(result) == 1

    def test_rerank_exception(self, clean_config):
        """rerank 异常处理。"""
        import wrapper.mem0_runtime as rt
        config = {
            "rerank": {
                "config": {
                    "api_key": "test_key",
                    "model": "test",
                },
                "provider": "openai",
            }
        }
        with mock.patch("wrapper.mem0_runtime.req.post", side_effect=Exception("network error")):
            result = rt.rerank("query", ["doc1"], config=config)
            assert result == []


class TestPathConfig:
    def test_home_dir(self):
        """HOME_DIR 配置。"""
        import wrapper.mem0_runtime as rt
        assert rt.HOME_DIR is not None
        assert rt.PROJECT_DIR is not None
