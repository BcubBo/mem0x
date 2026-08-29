"""NER 模型训练器 — 基于 spaCy 的增量训练，产出模型文件到 data/ner_models/。

阶段 2a：后台训练线程独立运行，不修改推理层（spacy_ner.py）。
训练完成后模型存到 data/ner_models/v{timestamp}/，等验证通过后再切换。
"""

import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("ner_trainer")

MODEL_DIR = "data/ner_models"
MIN_SAMPLES_TO_TRAIN = 100  # 至少 N 条样本才触发训练
EVAL_RATIO = 0.1            # 评估集比例
TRAIN_EPOCHS = 10           # 训练轮数
TRAIN_BATCH_SIZE = 8        # 每批样本数
MIN_F1_SAVE = 0.3           # F1 低于此值不保存模型


class NERTrainer:
    """spaCy NER 增量训练器。

    从预训练模型（zh_core_web_sm）出发，用弱监督标注数据微调。
    产出独立模型目录，不影响当前推理。
    """

    def __init__(self, model_dir: str = MODEL_DIR):
        self.model_dir = Path(model_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self._last_trained_count = 0  # 上次训练时的样本总数

    def should_train(self, current_sample_count: int) -> bool:
        """判断是否应该触发训练。

        条件：
        1. 样本数 >= MIN_SAMPLES_TO_TRAIN
        2. 自上次训练以来新增了 >= 50 条样本
        """
        if current_sample_count < MIN_SAMPLES_TO_TRAIN:
            return False
        new_since_last = current_sample_count - self._last_trained_count
        return new_since_last >= 50

    def train(self, samples: list[dict[str, Any]]) -> dict[str, Any] | None:
        """执行一次训练，返回模型元数据或 None（训练失败/跳过）。

        samples: [{"text": "...", "entities": [(start, end, "LABEL"), ...]}, ...]
        """
        if not samples:
            return None

        try:
            import spacy
            from spacy.training import Example
            from spacy.tokens import DocBin
        except ImportError:
            logger.error("spacy 未安装，跳过训练")
            return None

        # 加载预训练模型作为基础
        try:
            nlp = spacy.load("zh_core_web_sm")
        except Exception as e:
            logger.error("加载基础模型失败: %s", e)
            return None

        # 分割训练/评估集
        random.shuffle(samples)
        eval_size = max(1, int(len(samples) * EVAL_RATIO))
        eval_samples = samples[:eval_size]
        train_samples = samples[eval_size:]

        if not train_samples:
            logger.warning("训练样本不足，跳过")
            return None

        logger.info(
            "开始训练: train=%d eval=%d epochs=%d",
            len(train_samples), len(eval_samples), TRAIN_EPOCHS,
        )

        # 获取 NER pipeline
        if "ner" not in nlp.pipe_names:
            logger.error("基础模型没有 NER pipeline")
            return None
        ner = nlp.get_pipe("ner")

        # 添加新出现的标签
        all_labels = set()
        for s in train_samples + eval_samples:
            for _, _, label in s.get("entities", []):
                all_labels.add(label)
        for label in all_labels:
            ner.add_label(label)

        # 构建训练示例
        train_examples = self._build_examples(nlp, train_samples)
        eval_examples = self._build_examples(nlp, eval_samples)

        if not train_examples:
            logger.warning("无法构建训练示例，跳过")
            return None

        # 训练循环
        other_pipes = [pipe for pipe in nlp.pipe_names if pipe != "ner"]
        best_f1 = 0.0
        with nlp.disable_pipes(*other_pipes):
            optimizer = nlp.resume_training()
            for epoch in range(TRAIN_EPOCHS):
                random.shuffle(train_examples)
                losses = {}
                batches = spacy.util.minibatch(
                    train_examples, size=TRAIN_BATCH_SIZE
                )
                for batch in batches:
                    nlp.update(batch, drop=0.35, losses=losses, sgd=optimizer)

                # 每轮评估
                if eval_examples:
                    f1 = self._evaluate(nlp, eval_examples)
                    logger.info(
                        "  epoch %d/%d — loss=%.4f eval_f1=%.3f",
                        epoch + 1, TRAIN_EPOCHS, losses.get("ner", 0), f1,
                    )
                    best_f1 = max(best_f1, f1)

        # 最终评估（用全量 eval）
        if eval_examples:
            final_f1 = self._evaluate(nlp, eval_examples)
        else:
            final_f1 = best_f1

        logger.info("训练完成: final_f1=%.3f", final_f1)

        # F1 太低则不保存
        if final_f1 < MIN_F1_SAVE and len(eval_examples) > 5:
            logger.warning(
                "F1=%.3f < %.3f，不保存模型（样本质量不足）",
                final_f1, MIN_F1_SAVE,
            )
            return None

        # 保存模型
        version = f"v{int(time.time())}"
        model_path = self.model_dir / version
        nlp.to_disk(model_path)
        logger.info("模型已保存: %s", model_path)

        # 保存元数据
        meta = {
            "version": version,
            "f1": round(final_f1, 4),
            "train_samples": len(train_samples),
            "eval_samples": len(eval_samples),
            "total_samples": len(samples),
            "labels": sorted(all_labels),
            "epochs": TRAIN_EPOCHS,
            "base_model": "zh_core_web_sm",
            "created_at": time.time(),
            "model_path": str(model_path),
        }
        meta_path = model_path / "ner_meta.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        # 更新状态
        self._last_trained_count = len(samples)

        # 写入 latest 指针
        latest_path = self.model_dir / "latest.json"
        with open(latest_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        return meta

    def _build_examples(self, nlp, samples: list[dict]) -> list:
        """构建 spaCy Example 对象列表。"""
        import spacy
        from spacy.training import Example

        examples = []
        for s in samples:
            text = s.get("text", "")
            entities = s.get("entities", [])
            if not text:
                continue

            doc = nlp.make_doc(text)
            ents = []
            for start, end, label in entities:
                span = doc.char_span(start, end, label=label, alignment_mode="contract")
                if span is not None:
                    ents.append(span)

            doc.ents = spacy.util.filter_spans(ents)
            examples.append(Example.from_dict(doc, {"entities": [
                (e.start_char, e.end_char, e.label_) for e in doc.ents
            ]}))
        return examples

    def _evaluate(self, nlp, examples: list) -> float:
        """计算 F1 分数。"""
        from spacy.training import Example

        tp, fp, fn = 0, 0, 0
        for ex in examples:
            pred_doc = nlp(ex.reference.text)
            pred_ents = {(e.start_char, e.end_char, e.label_) for e in pred_doc.ents}
            gold_ents = {(e.start_char, e.end_char, e.label_) for e in ex.reference.ents}

            tp += len(pred_ents & gold_ents)
            fp += len(pred_ents - gold_ents)
            fn += len(gold_ents - pred_ents)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
        return f1

    def get_latest_model(self) -> dict | None:
        """读取 latest.json，返回最新模型元数据。"""
        latest_path = self.model_dir / "latest.json"
        if not latest_path.exists():
            return None
        try:
            with open(latest_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def list_models(self) -> list[dict]:
        """列出所有已训练模型。"""
        models = []
        for d in sorted(self.model_dir.iterdir()):
            if d.is_dir() and d.name.startswith("v"):
                meta_path = d / "ner_meta.json"
                if meta_path.exists():
                    try:
                        with open(meta_path, encoding="utf-8") as f:
                            models.append(json.load(f))
                    except Exception:
                        pass
        return models


# 全局单例
_trainer: NERTrainer | None = None


def get_trainer() -> NERTrainer:
    global _trainer
    if _trainer is None:
        _trainer = NERTrainer()
    return _trainer
