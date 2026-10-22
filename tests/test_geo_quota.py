"""地图调用配额：每秒 1 次 / 每日 100 次的强制限流与缓存。

配额是外部服务硬约束，超限必须优雅降级（不再打地图），而不是抛异常。
"""

import json
import sys
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from time_to_mcd import geo  # noqa: E402


class FakeGeo(geo.TencentGeo):
    """不联网的 TencentGeo：只替换出网口，保留真实的限流逻辑。

    注意不能替换 `_get`——那样会绕过 quota.allow()，就测不出限流了。
    """

    def __init__(self, quota, payload=None, wait_for_quota=False):
        # 默认关掉排队等待：这两个用例要验证的正是「超限立刻降级」
        super().__init__(key="fake-key", quota=quota, wait_for_quota=wait_for_quota)
        self.calls = 0
        self.payload = payload or {"routes": [{"distance": 800, "duration": 10, "steps": []}]}

    def _http_json(self, url):
        self.calls += 1  # 只有配额放行才会走到这里
        return self.payload


class GeoQuotaTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.id().replace(".", "_") + ".json")
        if self.path.exists():
            self.path.unlink()

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_second_limit_blocks_second_call(self):
        quota = geo.GeoQuota(path=self.path, max_per_second=1, max_per_day=100)
        fake = FakeGeo(quota)
        self.assertIsNotNone(fake.route((31.23, 121.47), (31.24, 121.48), "walk"))
        self.assertIsNone(fake.route((31.25, 121.49), (31.26, 121.50), "walk"))  # 同 1 秒内第 2 次被挡
        self.assertEqual(fake.calls, 1)
        self.assertIn("每秒", fake.blocked_reason)

    def test_second_window_rolls_forward(self):
        quota = geo.GeoQuota(path=self.path, max_per_second=1, max_per_day=100)
        fake = FakeGeo(quota)
        quota._second_hits = [time.time() - 1.5]  # 1.5 秒前的记录已滑出窗口
        self.assertTrue(quota.allow()[0])
        self.assertEqual(fake.calls, 0)

    def test_one_call_per_second_sustained(self):
        """每秒 1 次是可持续的：间隔足够就能一直放行（旧的每分钟 5 次做不到）。"""
        quota = geo.GeoQuota(path=self.path, max_per_second=1, max_per_day=100)
        for i in range(3):
            ok, reason = quota.allow()
            self.assertTrue(ok, reason)
            if i < 2:
                time.sleep(1.05)

    def test_daily_limit_blocks_and_persists(self):
        quota = geo.GeoQuota(path=self.path, max_per_second=1000, max_per_day=100)
        for _ in range(100):
            self.assertTrue(quota.allow()[0])
        ok, reason = quota.allow()
        self.assertFalse(ok)
        self.assertIn("当日上限", reason)

        # 当日计数落盘，重启进程（新实例读同一文件）不会重置
        again = geo.GeoQuota(path=self.path, max_per_second=1000, max_per_day=100)
        self.assertFalse(again.allow()[0])
        self.assertEqual(again.snapshot()["used_today"], 100)

    def test_day_rollover_resets(self):
        quota = geo.GeoQuota(path=self.path, max_per_second=1000, max_per_day=100)
        quota._state["used_today"] = 100
        quota._state["date"] = "2000-01-01"  # 跨天
        self.assertTrue(quota.allow()[0])
        self.assertEqual(quota.snapshot()["used_today"], 1)

    def test_cache_avoids_quota_consumption(self):
        quota = geo.GeoQuota(path=self.path, max_per_second=1000, max_per_day=100)
        fake = FakeGeo(quota)
        origin, dest = (31.2305, 121.4737), (31.2405, 121.4837)
        for _ in range(10):
            route = fake.route(origin, dest, "walk")
            self.assertIsNotNone(route)
        self.assertEqual(fake.calls, 1)  # 10 次查询只消耗 1 次配额
        self.assertIn("缓存", fake.route(origin, dest, "walk").note)

    def test_cache_key_tolerates_tiny_coord_drift(self):
        quota = geo.GeoQuota(path=self.path, max_per_second=1000, max_per_day=100)
        fake = FakeGeo(quota)
        fake.route((31.23051, 121.47371), (31.24050, 121.48370), "walk")
        fake.route((31.23052, 121.47372), (31.24050, 121.48370), "walk")  # 米级漂移
        self.assertEqual(fake.calls, 1)

    def test_blocked_route_falls_back_to_estimate(self):
        quota = geo.GeoQuota(path=self.path, max_per_second=1, max_per_day=100)
        fake = FakeGeo(quota)
        fake.route((31.20, 121.40), (31.21, 121.41), "walk")  # 用掉这一秒内唯一的额度
        route = geo.route_between((31.23, 121.47), (31.24, 121.48), 900.0, mode="walk", geo=fake)
        self.assertIsNotNone(route)  # 限流不抛异常
        self.assertEqual(route.source, "estimate")
        self.assertIn("每秒", route.note)

    def test_no_key_marks_estimate_reason(self):
        route = geo.route_between((31.23, 121.47), (31.24, 121.48), 900.0, mode="walk", geo=geo.TencentGeo("", quota=geo.GeoQuota(path=self.path)))
        self.assertEqual(route.source, "estimate")
        self.assertIn("T2M_TMAP_KEY", route.note)

    def test_snapshot_reports_limits(self):
        quota = geo.GeoQuota(path=self.path, max_per_second=1000, max_per_day=100)
        quota.allow()
        snap = quota.snapshot()
        self.assertEqual(snap["max_per_second"], 1000)
        self.assertEqual(snap["max_per_day"], 100)
        self.assertEqual(snap["left_today"], 99)

    # ------------------------------------------------------ 同名异地离群校验
    def test_rejects_absurdly_long_route(self):
        # 门店距离 255 m，真实路线却 299 km——地址被反查到江苏南京
        self.assertFalse(geo._is_plausible(299_300.0, 255.0))

    def test_accepts_normal_detour(self):
        self.assertTrue(geo._is_plausible(900.0, 255.0))  # 绕路属正常
        self.assertTrue(geo._is_plausible(1_200.0, 300.0))

    def test_geocode_falls_back_to_place_search(self):
        """腾讯配额按服务统计：geocoder 用完时 place 常仍有额度，解析能力不该整体失效。"""
        quota = geo.GeoQuota(path=self.path, max_per_second=10, max_per_day=100)

        class PlaceGeo(FakeGeo):
            def _http_json(self, url):
                self.calls += 1
                if "geocoder" in url:
                    return None  # 模拟地址解析已触配额
                return {"data": [{"title": "麦当劳越洋广场餐厅", "location": {"lat": 31.2266, "lng": 121.4455}}]}

        fake = PlaceGeo(quota)
        self.assertEqual(fake.geocode("麦当劳上海越洋广场餐厅", "上海"), (31.2266, 121.4455))

    def test_geocode_returns_none_when_both_fail(self):
        quota = geo.GeoQuota(path=self.path, max_per_second=10, max_per_day=100)
        fake = FakeGeo(quota, payload=None)
        self.assertIsNone(fake.geocode("某个地址", "上海"))

    def test_absurd_real_route_falls_back_to_estimate(self):
        quota = geo.GeoQuota(path=self.path, max_per_second=100, max_per_day=100)
        fake = FakeGeo(quota, payload={"routes": [{"distance": 299300, "duration": 4536, "steps": []}]})
        route = geo.route_between((31.2245, 121.4453), (32.0569, 118.7730), 255.0, mode="walk", geo=fake)
        self.assertEqual(route.source, "estimate")
        self.assertIn("路线异常", route.note)
        self.assertAlmostEqual(route.distance_m, 255.0, delta=400)  # 用回门店距离

    # ------------------------------------------------------------ 排队等待
    def test_wait_for_quota_queues_instead_of_giving_up(self):
        """一次查 3 家门店要连着算 3 条路线，秒级限流下排队能拿到真实路线。"""
        quota = geo.GeoQuota(path=self.path, max_per_second=1, max_per_day=100)
        fake = FakeGeo(quota, wait_for_quota=True)
        fake.max_wait = 2.0
        self.assertIsNotNone(fake.route((31.23, 121.47), (31.24, 121.48), "walk"))
        self.assertIsNotNone(fake.route((31.25, 121.49), (31.26, 121.50), "walk"))  # 等到了第 2 秒
        self.assertEqual(fake.calls, 2)

    def test_wait_gives_up_on_daily_limit(self):
        """当日额度用尽时等待没有意义，必须立刻放弃。"""
        quota = geo.GeoQuota(path=self.path, max_per_second=1000, max_per_day=1)
        quota.allow()
        self.assertFalse(quota.allow_wait(0.5)[0])

    # ------------------------------------------------------ 同名异地（城市前缀）
    def test_address_candidates_prefix_city(self):
        self.assertEqual(
            geo._address_candidates("吴江路169号", "上海"),
            ["上海市吴江路169号", "吴江路169号"],
        )

    def test_address_candidates_keeps_full_city(self):
        # 已经是「上海市」开头就不再重复拼前缀
        self.assertEqual(geo._address_candidates("上海市静安区吴江路169号", "上海"), ["上海市静安区吴江路169号"])
        self.assertEqual(geo._city_prefix("上海"), "上海市")
        self.assertEqual(geo._city_prefix("上海市"), "上海市")

    def test_geocode_retries_when_first_hit_is_wrong_city(self):
        """第一次解析命中同名异地（苏州/南京）时，换「城市+门牌」的写法重来。"""
        quota = geo.GeoQuota(path=self.path, max_per_second=10, max_per_day=100)

        class WrongCityGeo(FakeGeo):
            def _geocode_once(self, address, city=""):
                if address.startswith("上海市"):
                    return 31.2296, 121.4608  # 静安区吴江路（正确）
                return 31.1377, 120.6794  # 苏州（同名异地）

        fake = WrongCityGeo(quota)
        point = fake.geocode("吴江路169号", "上海", near=(31.2244, 121.4453), expected_m=234.0)
        self.assertEqual(point, (31.2296, 121.4608))

    def test_poisoned_cache_is_rechecked(self):
        """缓存里躺着一个荒谬坐标时，不能盲目相信（实测缓存过苏州/南京的错解）。"""
        quota = geo.GeoQuota(path=self.path, max_per_second=10, max_per_day=100)
        key = "geo:上海|吴江路169号"
        quota.cache_put(key, {"lat": 31.1377, "lng": 120.6794})  # 苏州

        class RightGeo(FakeGeo):
            def _geocode_once(self, address, city=""):
                return 31.2296, 121.4608

        fake = RightGeo(quota)
        point = fake.geocode("吴江路169号", "上海", near=(31.2244, 121.4453), expected_m=234.0)
        self.assertEqual(point, (31.2296, 121.4608))

    # ------------------------------------------------------------ 状态提示
    def test_status_121_explains_quota(self):
        text = geo._status_text(121, "此key每日调用量已达到上限")
        self.assertIn("121", text)
        self.assertIn("额度", text)  # 要让用户知道去控制台补配额，而不是以为程序坏了

    def test_blocked_reason_carries_tencent_status(self):
        """腾讯返回 121 时，界面要说出「额度用尽/未分配配额」，而不是干巴巴的估算。"""
        quota = geo.GeoQuota(path=self.path, max_per_second=10, max_per_day=100)
        fake = geo.TencentGeo("fake-key", quota=quota, wait_for_quota=False)

        class Resp:
            def read(self):
                return json.dumps({"status": 121, "message": "此key每日调用量已达到上限"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        original = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: Resp()
        try:
            self.assertIsNone(fake.geocode("上海市静安区吴江路169号", "上海"))
        finally:
            urllib.request.urlopen = original
        self.assertIn("121", fake.blocked_reason)
        self.assertIn("额度", fake.blocked_reason)

    def test_reverse_geocode_parses_address(self):
        quota = geo.GeoQuota(path=self.path, max_per_second=10, max_per_day=100)

        class RGeo(FakeGeo):
            def _http_json(self, url):
                self.calls += 1
                if "location=" in url:
                    return {"address": "上海市静安区愚园路132号"}
                return None

        fake = RGeo(quota)
        self.assertEqual(fake.reverse_geocode((31.2244, 121.4453)), "上海市静安区愚园路132号")
        # 第二次走缓存，不再消耗配额
        self.assertEqual(fake.reverse_geocode((31.2244, 121.4453)), "上海市静安区愚园路132号")
        self.assertEqual(fake.calls, 1)

    def test_reverse_geocode_builds_address_from_components(self):
        quota = geo.GeoQuota(path=self.path, max_per_second=10, max_per_day=100)

        class RGeo(FakeGeo):
            def _http_json(self, url):
                return {
                    "address_component": {
                        "province": "上海市",
                        "city": "上海市",
                        "district": "静安区",
                        "street": "愚园路",
                        "street_number": "132号",
                    }
                }

        self.assertEqual(RGeo(quota).reverse_geocode((31.22, 121.44)), "上海市上海市静安区愚园路132号")


if __name__ == "__main__":
    unittest.main()
