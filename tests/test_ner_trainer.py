"""ner_trainer 单元测试 — NER 模型训练器。"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestNERTrainerShouldTrain:
    """should_train 逻辑测试。"""

    def test_below_min_samples(self):
        """样本数不足时不训练。"""
        from wrapper.ner_trainer import NERTrainer

        with tempfile.TemporaryDirectory() as td:
            t = NERTrainer(model_dir=td)
            assert t.should_train(50) is False
            assert t.should_train(99) is False

    def test_first_train_at_min(self):
        """首次达到阈值时应训练。"""
        from wrapper.ner_trainer import NERTrainer

        with tempfile.TemporaryDirectory() as td:
            t = NERTrainer(model_dir=td)
            assert t.should_train(100) is True

    def test_no_new_samples_since_last(self):
        """自上次训练后无新样本，不训练。"""
        from wrapper.ner_trainer import NERTrainer

        with tempfile.TemporaryDirectory() as td:
            t = NERTrainer(model_dir=td)
            t._last_trained_count = 100
            assert t.should_train(100) is False
            assert t.should_train(149) is False

    def test_enough_new_samples(self):
        """新增 >= 50 条时应训练。"""
        from wrapper.ner_trainer import NERTrainer

        with tempfile.TemporaryDirectory() as td:
            t = NERTrainer(model_dir=td)
            t._last_trained_count = 100
            assert t.should_train(150) is True
            assert t.should_train(200) is True


class TestNERTrainerMeta:
    """模型元数据测试。"""

    def test_get_latest_no_model(self):
        """无模型时返回 None。"""
        from wrapper.ner_trainer import NERTrainer

        with tempfile.TemporaryDirectory() as td:
            t = NERTrainer(model_dir=td)
            assert t.get_latest_model() is None

    def test_list_models_empty(self):
        """无模型时返回空列表。"""
        from wrapper.ner_trainer import NERTrainer

        with tempfile.TemporaryDirectory() as td:
            t = NERTrainer(model_dir=td)
            assert t.list_models() == []

    def test_model_dir_created(self):
        """初始化时创建模型目录。"""
        from wrapper.ner_trainer import NERTrainer

        with tempfile.TemporaryDirectory() as td:
            model_dir = os.path.join(td, "ner_models")
            NERTrainer(model_dir=model_dir)
            assert os.path.isdir(model_dir)


class TestNERTrainerEvaluate:
    """_evaluate 逻辑测试。"""

    def test_perfect_f1(self):
        """完美预测时 F1 = 1.0。"""
        from wrapper.ner_trainer import NERTrainer

        with tempfile.TemporaryDirectory() as td:
            t = NERTrainer(model_dir=td)

            # mock nlp and examples
            mock_nlp = mock.MagicMock()
            mock_ex = mock.MagicMock()
            mock_ex.reference.ents = [
                mock.MagicMock(start_char=0, end_char=2, label_="PER"),
            ]
            mock_pred_doc = mock.MagicMock()
            mock_pred_doc.ents = [
                mock.MagicMock(start_char=0, end_char=2, label_="PER"),
            ]
            mock_nlp.return_value = mock_pred_doc

            f1 = t._evaluate(mock_nlp, [mock_ex])
            assert f1 == 1.0

    def test_zero_f1(self):
        """完全错误预测时 F1 = 0。"""
        from wrapper.ner_trainer import NERTrainer

        with tempfile.TemporaryDirectory() as td:
            t = NERTrainer(model_dir=td)

            mock_nlp = mock.MagicMock()
            mock_ex = mock.MagicMock()
            mock_ex.reference.ents = [
                mock.MagicMock(start_char=0, end_char=2, label_="PER"),
            ]
            mock_pred_doc = mock.MagicMock()
            mock_pred_doc.ents = []  # 没有预测到任何实体
            mock_nlp.return_value = mock_pred_doc

            f1 = t._evaluate(mock_nlp, [mock_ex])
            assert f1 == 0.0


class TestNERTrainerBuildExamples:
    """_build_examples 逻辑测试 — 需要真实 spaCy 模型。"""

    @pytest.fixture(autouse=True)
    def _load_nlp(self):
        try:
            import spacy
            self._nlp = spacy.load("zh_core_web_sm")
        except Exception:
            pytest.skip("zh_core_web_sm not available")

    def test_build_basic(self):
        """基本构建示例。"""
        from wrapper.ner_trainer import NERTrainer

        with tempfile.TemporaryDirectory() as td:
            t = NERTrainer(model_dir=td)
            samples = [
                {"text": "张三是工程师", "entities": [(0, 2, "PERSON")]},
            ]
            examples = t._build_examples(self._nlp, samples)
            assert len(examples) == 1

    def test_build_skip_empty(self):
        """跳过空文本。"""
        from wrapper.ner_trainer import NERTrainer

        with tempfile.TemporaryDirectory() as td:
            t = NERTrainer(model_dir=td)
            samples = [
                {"text": "", "entities": [(0, 2, "PERSON")]},
                {"text": "ok", "entities": []},
            ]
            examples = t._build_examples(self._nlp, samples)
            # 空文本跳过，空实体列表产出空 ents 的 Example
            assert len(examples) <= 1
