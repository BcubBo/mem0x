"""auto_expire 单元测试 — 过期判断、线程管理。"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestIsExpired:
    def test_expired_with_past_date(self):
        """显式过期日期在过去 → 过期。"""
        from wrapper.auto_expire import _is_expired
        past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        data = f"some content [expires:{past}]"
        assert _is_expired(data, datetime.now(timezone.utc).isoformat()) is True

    def test_not_expired_with_future_date(self):
        """显式过期日期在未来 → 未过期。"""
        from wrapper.auto_expire import _is_expired
        future = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%d")
        data = f"content [expires:{future}]"
        assert _is_expired(data, datetime.now(timezone.utc).isoformat()) is False

    def test_no_expires_tag(self):
        """无 expires 标签 → 未过期。"""
        from wrapper.auto_expire import _is_expired
        assert _is_expired("hello world", datetime.now(timezone.utc).isoformat()) is False

    def test_lane_identity_never_expires(self):
        """identity lane 永不衰减。"""
        from wrapper.auto_expire import _is_expired
        created = (datetime.now(timezone.utc) - timedelta(days=3650)).isoformat()
        assert _is_expired("[lane:identity] some content", created) is False

    def test_lane_emotion_expires(self):
        """emotion lane 5 天过期。"""
        from wrapper.auto_expire import _is_expired
        created = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        assert _is_expired("[lane:emotion] some emotion", created) is True

    def test_lane_emotion_not_expired(self):
        """emotion lane 未过期。"""
        from wrapper.auto_expire import _is_expired
        created = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        assert _is_expired("[lane:emotion] some emotion", created) is False

    def test_lane_project_180_days(self):
        """project lane 180 天过期。"""
        from wrapper.auto_expire import _is_expired
        created = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        assert _is_expired("[lane:project] project data", created) is True

    def test_lane_project_not_expired(self):
        """project lane 未过期。"""
        from wrapper.auto_expire import _is_expired
        created = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        assert _is_expired("[lane:project] project data", created) is False

    def test_lane_default_30_days(self):
        """default lane 30 天过期。"""
        from wrapper.auto_expire import _is_expired
        created = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        assert _is_expired("[lane:default] some data", created) is True

    def test_lane_preference_never_expires(self):
        """preference lane 永不衰减。"""
        from wrapper.auto_expire import _is_expired
        created = (datetime.now(timezone.utc) - timedelta(days=3650)).isoformat()
        assert _is_expired("[lane:preference] preference data", created) is False

    def test_no_created_at(self):
        """无 created_at → 未过期。"""
        from wrapper.auto_expire import _is_expired
        assert _is_expired("[lane:default] data", None) is False

    def test_invalid_expires_date(self):
        """无效 expires 日期 → 跳过。"""
        from wrapper.auto_expire import _is_expired
        assert _is_expired("content [expires:invalid]", None) is False


class TestAutoExpireThread:
    def test_is_running_initial(self):
        """初始状态。"""
        import wrapper.auto_expire as ae
        ae._running = False
        ae._thread = None
        assert ae.is_running() is False

    def test_stop(self):
        """停止。"""
        import wrapper.auto_expire as ae
        ae._running = False
        ae._thread = None
        ae.stop()
        assert ae._running is False

    def test_default_interval(self):
        """默认间隔。"""
        from wrapper.auto_expire import DEFAULT_INTERVAL, BATCH_SIZE, MAX_SCROLL_ROUNDS
        assert DEFAULT_INTERVAL == 3600
        assert BATCH_SIZE == 200
        assert MAX_SCROLL_ROUNDS == 200


class TestLaneTTL:
    def test_lane_ttl_mapping(self):
        """lane TTL 映射。"""
        from wrapper.auto_expire import _LANE_TTL
        assert _LANE_TTL["identity"] is None
        assert _LANE_TTL["preference"] is None
        assert _LANE_TTL["project"] == 180
        assert _LANE_TTL["emotion"] == 5
        assert _LANE_TTL["default"] == 30
