"""降级追踪器 — 记录系统组件降级状态，5分钟自愈。

"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List

logger = logging.getLogger("mem0x.degradation")


class DegradationTracker:
    """线程安全的降级状态追踪器。

    用法:
        DegradationTracker.record_degradation("reranker", "SiliconFlow 400")
        # ... 组件恢复后 ...
        DegradationTracker.clear_degradation("reranker")
    """

    _lock = threading.Lock()
    _degraded_map: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def record_degradation(cls, component: str, reason: str, *, severity: str = "warning") -> None:
        with cls._lock:
            cls._degraded_map[component] = {
                "component": component,
                "reason": str(reason)[:200],
                "severity": severity,
                "timestamp": time.time(),
                "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            }
        logger.warning("⚠️ [Degradation] %s: %s", component, reason)

    @classmethod
    def clear_degradation(cls, component: str) -> None:
        with cls._lock:
            cls._degraded_map.pop(component, None)

    @classmethod
    def get_degraded_components(cls) -> List[str]:
        """返回当前处于降级状态的组件名列表（5分钟内无新事件视为自愈）。

        NOTE: 过期条目不主动删除（by design）——_degraded_map 是覆盖式状态表，
        每个 component 只保留最新一条，条目数上限 = component 数量（通常<10）。
        留着可查最近一次降级时间和原因，内存开销可忽略。
        """
        with cls._lock:
            now = time.time()
            return [
                comp for comp, info in cls._degraded_map.items()
                if (now - info["timestamp"]) < 300
            ]

    @classmethod
    def get_degraded_details(cls) -> List[Dict[str, Any]]:
        with cls._lock:
            now = time.time()
            return [
                info for info in cls._degraded_map.values()
                if (now - info["timestamp"]) < 300
            ]
