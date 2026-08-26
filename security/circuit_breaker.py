"""简单断路器：追踪外部服务（Qdrant/Neo4j/LLM）的健康状态。

状态机：CLOSED → (连续失败≥阈值) → OPEN → (超时后) → HALF_OPEN → (成功) → CLOSED
"""
import logging
import time
import threading
from enum import Enum

logger = logging.getLogger("mem0x.circuit_breaker")


class State(Enum):
    CLOSED = "closed"        # 正常，允许请求
    OPEN = "open"            # 熔断，拒绝请求
    HALF_OPEN = "half_open"  # 试探，允许1个请求


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 5, recovery_timeout: float = 30):
        """
        Args:
            name: 服务名称（用于日志）
            failure_threshold: 连续失败多少次触发熔断
            recovery_timeout: 熔断后多久进入半开状态（秒）
        """
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = State.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> State:
        with self._lock:
            if self._state == State.OPEN:
                if time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._state = State.HALF_OPEN
                    logger.info("断路器 %s: OPEN → HALF_OPEN", self.name)
            return self._state

    def allow_request(self) -> bool:
        """是否允许本次请求通过。"""
        s = self.state
        return s in (State.CLOSED, State.HALF_OPEN)

    def record_success(self):
        """记录成功。"""
        with self._lock:
            if self._state == State.HALF_OPEN:
                self._state = State.CLOSED
                logger.info("断路器 %s: HALF_OPEN → CLOSED（恢复）", self.name)
            self._failure_count = 0

    def record_failure(self):
        """记录失败。"""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._state == State.CLOSED and self._failure_count >= self.failure_threshold:
                self._state = State.OPEN
                logger.warning("断路器 %s: CLOSED → OPEN（连续失败%d次）", self.name, self._failure_count)
            elif self._state == State.HALF_OPEN:
                self._state = State.OPEN
                logger.warning("断路器 %s: HALF_OPEN → OPEN（试探失败）", self.name)

    def stats(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
        }


# 全局断路器实例
qdrant_breaker = CircuitBreaker("qdrant", failure_threshold=5, recovery_timeout=30)
neo4j_breaker = CircuitBreaker("neo4j", failure_threshold=5, recovery_timeout=30)
llm_breaker = CircuitBreaker("llm", failure_threshold=3, recovery_timeout=60)
