"""evolve_mem 单元测试 — 记忆自进化。"""
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestEvolveMemConfig:
    def test_default_config(self):
        """默认配置。"""
        from wrapper.evolve_mem import THRESHOLD_HIGH, THRESHOLD_LOW, DEFAULT_INTERVAL
        assert THRESHOLD_HIGH == 0.7
        assert THRESHOLD_LOW == 0.3
        assert isinstance(DEFAULT_INTERVAL, int)


class TestEvolveMemThread:
    def test_is_running_initial(self):
        """初始状态。"""
        import wrapper.evolve_mem as em
        em._running = False
        em._thread = None
        assert em.is_running() is False

    def test_stop(self):
        """停止。"""
        import wrapper.evolve_mem as em
        em._running = True
        em.stop()
        assert em._running is False

    def test_start_stop(self):
        """启动和停止。"""
        import wrapper.evolve_mem as em
        em._running = False
        em._thread = None

        def mock_getter():
            return None

        em.start(mock_getter, interval=1)
        assert em._running is True
        em.stop()
        import time
        time.sleep(0.1)
        assert em._running is False
