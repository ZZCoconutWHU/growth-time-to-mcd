"""Web 服务 API 单元测试：覆盖 /api/order（MCP 4.2.13 查询订单详情）。

不连外网、不依赖真实 Token：
  - 无 Token → mock 演示分支，验证返回结构与标志位（demo=true / connected=false）
  - build_service 抛错 → 验证异常分支仍返回结构完整、order 置空、带回 error
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from time_to_mcd.web_app import Handler  # noqa: E402


class _DummyHandler(Handler):
    """屏蔽网络/文件依赖，仅用于调用 _api_order（该方法不引用 self）。"""

    def __init__(self):
        pass


class TestApiOrder(unittest.TestCase):
    def _call_with_token(self, token):
        """按指定 Token 调用 _api_order。

        必须连 `.env` 一起隔离：_service() 走 config.load_settings()，
        只改 os.environ 的话，本机 .env 里的真实 Token 仍会被读进来，
        导致「无 Token」用例在配好 .env 的机器上误判为已连接。
        """
        from time_to_mcd.config import Settings

        settings = Settings(url="https://mcp.mcd.cn", token=token or "")
        with mock.patch("time_to_mcd.config.load_settings", return_value=settings):
            return Handler._api_order(_DummyHandler())

    def test_parse_origin_rejects_bad_values(self):
        from time_to_mcd.web_app import _parse_origin

        self.assertEqual(_parse_origin([31.23, 121.47]), (31.23, 121.47))
        for bad in (None, [], [1], "31,121", ["a", "b"], [91.0, 121.0], [31.0, 300.0]):
            self.assertIsNone(_parse_origin(bad), f"非法定位值应被拒绝：{bad!r}")

    def test_demo_mode_no_token(self):
        data = self._call_with_token(None)
        self.assertFalse(data["connected"], "无 Token 应为未连接")
        self.assertTrue(data["demo"], "无 Token 应走演示分支")
        self.assertIsNotNone(data["order"], "mock 分支应返回演示订单")
        o = data["order"]
        for key in ("order_id", "status_text", "stage_label", "mode_label",
                    "store_name", "pickup_code", "amount", "items",
                    "progress", "is_final"):
            self.assertIn(key, o, f"订单缺少字段 {key}")
        self.assertIsInstance(o["progress"], int)
        self.assertGreaterEqual(o["progress"], 0)
        self.assertLessEqual(o["progress"], 100)
        self.assertIn("eta", data)
        self.assertIn("plan", data)
        self.assertIn("remaining_min", data["eta"])

    def test_error_branch_returns_structure(self):
        with mock.patch("time_to_mcd.service.build_service", side_effect=RuntimeError("boom")):
            data = self._call_with_token(None)
        self.assertFalse(data["connected"])
        self.assertTrue(data["demo"])
        self.assertIsNone(data["order"], "异常分支 order 应置空")
        self.assertIn("error", data, "异常分支应带回 error 信息")

    def test_real_mode_flag_set_when_token_present(self):
        # 让 build_service 直接抛出，验证标志位：connected=true / demo=false
        # （不真正联网，只断言返回结构里的连接标志）
        with mock.patch("time_to_mcd.service.build_service", side_effect=RuntimeError("no-net")):
            data = self._call_with_token("fake-token")
        self.assertTrue(data["connected"], "有 Token 应标记已连接")
        self.assertFalse(data["demo"], "有 Token 不应是演示模式")
        self.assertIsNone(data["order"])
        self.assertIn("error", data)


class TestApiStores(unittest.TestCase):
    """/api/stores：定位点要能被翻译成看得懂的地名，且不因地图异常崩掉。"""

    def _make_service(self, reverse="上海市静安区愚园路132号"):
        from types import SimpleNamespace

        from time_to_mcd.geo import Route

        store = SimpleNamespace(
            code="0001", name="麦当劳上海吴江路餐厅", address="吴江路169号",
            hours="06:30-22:00", status="营业中", is_open=True,
            distance_m=234.0, distance_text="234 m", services={"wifi"},
        )
        route = Route(distance_m=1805.0, minutes=27.0, mode="walk", source="tencent")

        class GeoStub:
            available = True

            def reverse_geocode(self, point):
                return reverse

            def quota_snapshot(self):
                return {"available": True, "used_today": 3, "max_per_day": 100, "left_today": 97}

        return SimpleNamespace(
            find_stores=lambda *a, **k: [store],
            profile_or_default=lambda: SimpleNamespace(weight_kg=65.0),
            commute=lambda *a, **k: route,
            geo=GeoStub(),
        )

    def _call(self, body):
        from time_to_mcd.config import Settings

        settings = Settings(url="https://mcp.mcd.cn", token="fake", protocol_version="2025-06-18", timeout=30)
        with mock.patch("time_to_mcd.web_app._service", return_value=(self._make_service(), settings)):
            return Handler._api_stores(_DummyHandler(), body)

    def test_origin_label_from_reverse_geocode(self):
        data = self._call({"address": "上海市静安区南京西路", "origin": [31.2244, 121.4453]})
        self.assertEqual(data["origin_label"], "上海市静安区愚园路132号")
        self.assertEqual(data["stores"][0]["commute"]["source"], "tencent")
        self.assertEqual(data["quota"]["left_today"], 97)

    def test_origin_label_empty_when_reverse_fails(self):
        self._make_service()  # 默认桩
        from unittest import mock as _m

        svc = self._make_service(reverse=None)
        from types import SimpleNamespace

        from time_to_mcd.config import Settings

        settings = Settings(url="https://mcp.mcd.cn", token="fake", protocol_version="2025-06-18", timeout=30)
        with _m.patch("time_to_mcd.web_app._service", return_value=(svc, settings)):
            data = Handler._api_stores(_DummyHandler(), {"address": "上海", "origin": [31.22, 121.44]})
        self.assertEqual(data["origin_label"], "", "逆地址解析失败时只留空，不能影响门店列表")

    def test_missing_address_rejected(self):
        data = self._call({"address": "  "})
        self.assertIn("error", data)
        self.assertEqual(data["stores"], [])


if __name__ == "__main__":
    unittest.main()
