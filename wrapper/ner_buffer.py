"""NER 训练数据采集缓冲区 — 线程安全的 deque，用于暂存 tags_hook 产出的样本。

训练管线阶段1（数据采集层）：tags_hook 写入时 push 到缓冲区，
后台训练线程通过 drain() 批量取出进行弱监督标注和训练。
"""

import logging
import threading
import time
from collections import deque
from typing import Any

logger = logging.getLogger("ner_buffer")

# 最大缓冲条目数（超出时丢弃最旧的）
MAX_BUFFER_SIZE = 5000


class NERBuffer:
    """线程安全的 NER 训练样本缓冲区。"""

    def __init__(self, max_size: int = MAX_BUFFER_SIZE):
        self._buf: deque[dict[str, Any]] = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._push_count = 0
        self._drop_count = 0

    def push(self, text: str, entities: list[dict[str, Any]]) -> None:
        """推入一条训练样本。

        entities: [{"text": "张三", "label": "PERSON"}, ...]
        """
        if not text or not entities:
            return
        sample = {
            "text": text[:10000],
            "entities": entities,
            "ts": time.time(),
        }
        with self._lock:
            if len(self._buf) >= self._buf.maxlen:
                self._drop_count += 1
            self._buf.append(sample)
            self._push_count += 1
        logger.debug("ner_buffer push: entities=%d buf_size=%d", len(entities), len(self._buf))

    def drain(self, max_items: int = 50) -> list[dict[str, Any]]:
        """取出并清空最多 max_items 条样本（FIFO）。"""
        items = []
        with self._lock:
            for _ in range(min(max_items, len(self._buf))):
                items.append(self._buf.popleft())
        if items:
            logger.info("ner_buffer drain: %d items (total_pushed=%d, dropped=%d)",
                        len(items), self._push_count, self._drop_count)
        return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._buf),
                "total_pushed": self._push_count,
                "total_dropped": self._drop_count,
            }


# 全局单例
_buffer: NERBuffer | None = None


def get_buffer() -> NERBuffer:
    global _buffer
    if _buffer is None:
        _buffer = NERBuffer()
    return _buffer
