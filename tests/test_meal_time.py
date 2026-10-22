"""按当前时间自动落到 早餐/午餐/晚餐/加餐。

时间取麦当劳 MCP 的 now-time-info（服务端时间），拿不到才用本机时间，
且必须在界面上如实标注来源，不能把本机时间说成服务端时间。
"""

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from time_to_mcd import coach  # noqa: E402


class MealOfTimeTests(unittest.TestCase):
    def _at(self, hour, minute=0):
        return coach.meal_of_time(datetime(2026, 10, 22, hour, minute))

    def test_breakfast_window(self):
        self.assertEqual(self._at(5, 0), "breakfast")
        self.assertEqual(self._at(7, 30), "breakfast")
        self.assertEqual(self._at(10, 29), "breakfast")

    def test_lunch_starts_at_mcd_switch_time(self):
        """10:30 是麦当劳早餐切换正餐的时点。"""
        self.assertEqual(self._at(10, 30), "lunch")
        self.assertEqual(self._at(12, 0), "lunch")
        self.assertEqual(self._at(14, 29), "lunch")

    def test_afternoon_snack(self):
        self.assertEqual(self._at(14, 30), "snack")
        self.assertEqual(self._at(16, 0), "snack")

    def test_dinner_window(self):
        self.assertEqual(self._at(17, 0), "dinner")
        self.assertEqual(self._at(20, 0), "dinner")
        self.assertEqual(self._at(21, 29), "dinner")

    def test_late_night_snack(self):
        self.assertEqual(self._at(21, 30), "snack")
        self.assertEqual(self._at(23, 59), "snack")
        self.assertEqual(self._at(0, 30), "snack")  # 凌晨算前一晚的夜宵
        self.assertEqual(self._at(4, 59), "snack")

    def test_window_text(self):
        self.assertEqual(coach.meal_window_text("breakfast"), "05:00–10:30")
        self.assertEqual(coach.meal_window_text("lunch"), "10:30–14:30")
        self.assertEqual(coach.meal_window_text("dinner"), "17:00–21:30")
        # 加餐有两段：下午茶 + 夜宵
        self.assertEqual(coach.meal_window_text("snack"), "14:30–17:00、21:30–05:00")

    def test_every_meal_reachable(self):
        """四个餐次都得能被时间规则命中，不能有永远选不到的死选项。"""
        hit = {coach.meal_of_time(datetime(2026, 1, 1, h)) for h in range(24)}
        self.assertEqual(hit, {"breakfast", "lunch", "dinner", "snack"})


class CurrentMealTests(unittest.TestCase):
    """时间来源必须可区分：mcp（服务端） vs local（本机）。"""

    def _service(self, payload, raises=False):
        from time_to_mcd.service import CompassService

        svc = CompassService.__new__(CompassService)
        if raises:
            svc._call = mock.Mock(side_effect=RuntimeError("no-net"))
        else:
            svc._call = mock.Mock(return_value=payload)
        return svc

    def test_uses_mcp_server_time(self):
        svc = self._service({"time": "2026-10-22 20:00:00"})
        info = svc.current_meal()
        self.assertEqual(info["source"], "mcp")
        self.assertEqual(info["meal"], "dinner")
        self.assertEqual(info["meal_label"], "晚餐")
        self.assertEqual(info["window"], "17:00–21:30")

    def test_falls_back_to_local_and_says_so(self):
        svc = self._service(None, raises=True)
        info = svc.current_meal()
        self.assertEqual(info["source"], "local")
        self.assertIn(info["meal"], ("breakfast", "lunch", "dinner", "snack"))

    def test_real_now_time_info_shape(self):
        """实测返回：时间既在顶层 datetime，也在 data 子对象里。

        之前只找顶层的 timestamp/time/currentTime/serverTime，全部落空 →
        静默退回本机时间，界面上还看不出来。这里按真实结构锁死。
        """
        payload = {
            "success": True,
            "code": 200,
            "message": "请求成功",
            "datetime": "2026-10-22 20:20:36",
            "data": {
                "timestamp": 1792671636469,
                "datetime": "2026-10-22T20:20:36.469",
                "formatted": "2026-10-22 20:20:36",
                "timezone": "GMT+08:00",
            },
        }
        svc = self._service(payload)
        self.assertEqual(svc.now_with_source()[0].hour, 20)
        self.assertEqual(svc.now_with_source()[1], "mcp")
        self.assertEqual(svc.current_meal()["meal"], "dinner")

    def test_breakfast_from_server_time(self):
        svc = self._service({"data": {"formatted": "2026-10-22 08:15:00"}})
        info = svc.current_meal()
        self.assertEqual(info["source"], "mcp")
        self.assertEqual(info["meal"], "breakfast")
        self.assertEqual(info["window"], "05:00–10:30")

    def test_timestamp_millis_supported(self):
        svc = self._service({"timestamp": 1761134400000})  # 2026-05-23 12:00 (UTC+8)
        info = svc.current_meal()
        self.assertEqual(info["source"], "mcp")
        self.assertIn(info["meal"], ("lunch", "dinner"))  # 时区不同只影响落在哪一段


class ApiNowTests(unittest.TestCase):
    def test_now_endpoint_shape(self):
        from time_to_mcd.web_app import Handler

        class _Dummy(Handler):
            def __init__(self):
                pass

        class _Svc:
            def current_meal(self):
                return {
                    "meal": "breakfast",
                    "meal_label": "早餐",
                    "time": "2026-10-22 08:15",
                    "hour": 8,
                    "source": "mcp",
                    "window": "05:00–10:30",
                }

        class _Settings:
            token = "fake"

        with mock.patch("time_to_mcd.web_app._service", return_value=(_Svc(), _Settings())):
            data = Handler._api_now(_Dummy())
        self.assertEqual(data["meal"], "breakfast")
        self.assertEqual(data["source"], "mcp")
        self.assertTrue(data["connected"])

    def test_now_endpoint_never_crashes(self):
        from time_to_mcd.web_app import Handler

        class _Dummy(Handler):
            def __init__(self):
                pass

        with mock.patch("time_to_mcd.web_app._service", side_effect=RuntimeError("boom")):
            data = Handler._api_now(_Dummy())
        self.assertEqual(data["source"], "local")  # 时间拿不到也要能继续用页面
        self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
