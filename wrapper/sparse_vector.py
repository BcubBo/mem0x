"""BM25 Sparse Vector 生成器 — 将文本转为 Qdrant sparse vector 格式。

用于 Qdrant hybrid search，替代 FTS5 的关键词搜索能力。
IDF 统计持久化到 SQLite，重启不丢失。
"""
import json
import logging
import math
import os
import re
import sqlite3
import threading
from collections import Counter
from typing import Any

logger = logging.getLogger("mem0x.sparse_vector")

# SQLite 路径（挂载 volume，重启不丢）
_IDF_DB = os.environ.get("MEM0X_DATA_DIR", "/app/data") + "/bm25_idf.db"

# CJK + ASCII 分词（与 FTS5 unicode61 兼容）
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+")

# BM25 参数
_K1 = 1.5   # 词频饱和参数
_B = 0.75   # 文档长度归一化参数

# 停用词（可从 config 加载，当前硬编码常用词）
_STOP_WORDS = frozenset({
    "的", "了", "在", "是", "和", "有", "为", "这", "中", "不", "也",
    "与", "或", "被", "将", "把", "从", "到", "对", "等", "能", "可",
    "会", "要", "没", "过", "后", "前", "时", "着", "地", "得",
    "the", "a", "an", "is", "are", "was", "in", "on", "at", "to",
    "for", "of", "with", "by", "as", "it", "its", "be", "do",
})


def tokenize(text: str) -> list[str]:
    """分词：CJK 单字/词 + ASCII 单词，过滤停用词和单字符。"""
    tokens = _TOKEN_RE.findall(text)
    return [t for t in tokens if len(t) >= 2 and t.lower() not in _STOP_WORDS]


class BM25Encoder:
    """BM25 sparse vector 编码器。维护全局 IDF 统计。"""

    def __init__(self, k1: float = _K1, b: float = _B):
        self.k1 = k1
        self.b = b
        self.doc_count = 0
        self.doc_freq: Counter[str] = Counter()  # 每个词出现在多少文档中
        self.total_length = 0  # 所有文档总长度

    def _update_stats(self, tokens: list[str]) -> None:
        """更新全局统计（增量），每100篇自动持久化。"""
        self.doc_count += 1
        self.total_length += len(tokens)
        unique = set(tokens)
        for t in unique:
            self.doc_freq[t] += 1
        # 每100篇持久化一次（避免频繁写DB）
        if self.doc_count % 100 == 0:
            self.save_idf()

    def _idf(self, term: str) -> float:
        """计算 IDF：log((N - n + 0.5) / (n + 0.5) + 1)。"""
        n = self.doc_freq.get(term, 0)
        N = max(self.doc_count, 1)
        return math.log((N - n + 0.5) / (n + 0.5) + 1)

    def _avg_dl(self) -> float:
        return self.total_length / max(self.doc_count, 1)

    def encode(self, text: str) -> dict[str, Any]:
        """将文本编码为 Qdrant sparse vector 格式。"""
        tokens = tokenize(text)
        if not tokens:
            return {"indices": [], "values": []}

        self._update_stats(tokens)
        tf = Counter(tokens)
        avg_dl = self._avg_dl()
        dl = len(tokens)

        indices = []
        values = []
        for term, count in tf.items():
            idf = self._idf(term)
            tf_norm = (count * (self.k1 + 1)) / (count + self.k1 * (1 - self.b + self.b * dl / max(avg_dl, 1)))
            score = idf * tf_norm
            if score > 0:
                indices.append(hash(term) % (2**31))  # 简单 hash 做索引
                values.append(round(score, 6))

        return {"indices": indices, "values": values}

    def encode_query(self, query: str) -> dict[str, Any]:
        """编码查询文本为 sparse vector（只用 IDF，不用 TF）。"""
        tokens = tokenize(query)
        if not tokens:
            return {"indices": [], "values": []}

        tf = Counter(tokens)
        indices = []
        values = []
        for term, count in tf.items():
            idf = self._idf(term)
            if idf > 0:
                indices.append(hash(term) % (2**31))
                values.append(round(idf * count, 6))

        return {"indices": indices, "values": values}

    def reset_stats(self) -> None:
        """重置全局统计（回填完成后调用）。"""
        self.doc_count = 0
        self.doc_freq.clear()
        self.total_length = 0

    def save_idf(self) -> None:
        """持久化 IDF 统计到 SQLite（挂载 volume，重启不丢）。"""
        try:
            conn = sqlite3.connect(_IDF_DB)
            conn.execute("CREATE TABLE IF NOT EXISTS bm25_idf (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("REPLACE INTO bm25_idf VALUES (?, ?)", ("doc_count", str(self.doc_count)))
            conn.execute("REPLACE INTO bm25_idf VALUES (?, ?)", ("total_length", str(self.total_length)))
            conn.execute("REPLACE INTO bm25_idf VALUES (?, ?)", ("doc_freq", json.dumps(dict(self.doc_freq), ensure_ascii=False)))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("BM25 IDF 持久化失败: %s", e)

    def load_idf(self) -> bool:
        """从 SQLite 加载 IDF 统计。返回是否成功。"""
        try:
            if not os.path.exists(_IDF_DB):
                return False
            conn = sqlite3.connect(_IDF_DB)
            rows = dict(conn.execute("SELECT key, value FROM bm25_idf").fetchall())
            conn.close()
            if not rows:
                return False
            self.doc_count = int(rows.get("doc_count", 0))
            self.total_length = int(rows.get("total_length", 0))
            self.doc_freq = Counter(json.loads(rows.get("doc_freq", "{}")))
            logger.info("BM25 IDF 加载成功: %d 文档, %d 词", self.doc_count, len(self.doc_freq))
            return True
        except Exception as e:
            logger.warning("BM25 IDF 加载失败: %s", e)
            return False


# 全局单例
_bm25_encoder: BM25Encoder | None = None
_bm25_encoder_lock = threading.Lock()


def get_bm25_encoder() -> BM25Encoder:
    global _bm25_encoder
    if _bm25_encoder is None:
        with _bm25_encoder_lock:
            if _bm25_encoder is None:
                _bm25_encoder = BM25Encoder()
                _bm25_encoder.load_idf()  # 启动时自动加载
    return _bm25_encoder
