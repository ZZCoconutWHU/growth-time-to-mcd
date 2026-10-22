"""引擎层单元测试：不依赖网络，直接跑 python -m unittest discover tests"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from time_to_mcd import nutrition as nut  # noqa: E402
from time_to_mcd import places as plc  # noqa: E402
from time_to_mcd import wait as wat  # noqa: E402


class ParseItemsTest(unittest.TestCase):
    def test_colon_and_multiplier(self):
        self.assertEqual(
            nut.parse_items("巨无霸:1,中薯条:2"),
            [("巨无霸", 1.0), ("中薯条", 2.0)],
        )

    def test_x_multiplier_and_plain(self):
        self.assertEqual(nut.parse_items("板烧鸡腿堡x2，玉米杯"), [("板烧鸡腿堡", 2.0), ("玉米杯", 1.0)])

    def test_fullwidth_colon(self):
        self.assertEqual(nut.parse_items("麦乐鸡：1"), [("麦乐鸡", 1.0)])


class NutritionTest(unittest.TestCase):
    def setUp(self):
        self.catalog = [
            nut.Food("巨无霸", energy=550, protein=25, fat=30, carb=42, sodium=1000),
            nut.Food("板烧鸡腿堡", energy=400, protein=27, fat=14, carb=41, sodium=820),
            nut.Food("中薯条", energy=340, protein=4, fat=16, carb=44, sodium=250),
            nut.Food("小薯条", energy=230, protein=3, fat=11, carb=30, sodium=170),
            nut.Food("中杯无糖可乐", energy=0, protein=0, fat=0, carb=0, sodium=10),
            nut.Food("中杯可口可乐", energy=200, protein=0, fat=0, carb=50, sodium=15),
        ]

    def test_match_by_fuzzy_name(self):
        self.assertEqual(nut.match_food("巨无霸", self.catalog).name, "巨无霸")
        self.assertEqual(nut.match_food("板烧", self.catalog).name, "板烧鸡腿堡")

    def test_unmatched_returns_none(self):
        self.assertIsNone(nut.match_food("佛跳墙", self.catalog, threshold=0.6))

    def test_analyze_totals_and_budget_flag(self):
        items = [nut.LineItem(self.catalog[0]), nut.LineItem(self.catalog[2])]
        report = nut.analyze(items, budget=700)
        self.assertAlmostEqual(report.total_energy, 890)
        self.assertAlmostEqual(report.total_sodium, 1250)
        self.assertTrue(any(f.level == "high" and "超出预算" in f.text for f in report.flags))

    def test_fat_ratio(self):
        items = [nut.LineItem(self.catalog[0])]
        report = nut.analyze(items)
        self.assertGreater(report.fat_energy_ratio, 40)

    def test_no_sugar_drink_not_suggested_to_go_sugarfree(self):
        items = [nut.LineItem(self.catalog[4])]
        titles = [s.title for s in nut.swap_suggestions(items, self.catalog)]
        self.assertNotIn("饮料换无糖", titles)

    def test_lighter_alternative_uses_catalog(self):
        items = [nut.LineItem(self.catalog[2])]
        suggestions = nut.swap_suggestions(items, self.catalog)
        self.assertTrue(any("小薯条" in s.title for s in suggestions))


class WaitTest(unittest.TestCase):
    def test_detect_stage(self):
        self.assertEqual(wat.detect_stage("制作中"), wat.S_COOKING)
        self.assertEqual(wat.detect_stage("已出餐，请取餐"), wat.S_READY)
        self.assertEqual(wat.detect_stage("订单已取消"), wat.S_CANCELLED)
        self.assertEqual(wat.detect_stage("骑手已取货，配送中"), wat.S_DELIVERING)

    def test_eta_uses_promise_time(self):
        now = datetime(2026, 10, 22, 12, 0, 0)
        order = wat.Order(order_id="X", stage=wat.S_COOKING, promise_at=now + timedelta(minutes=8))
        eta = wat.estimate_eta(order, now)
        self.assertEqual(eta.confidence, "high")
        self.assertAlmostEqual(eta.remaining_min, 8.0, places=1)

    def test_eta_fallback_is_medium_confidence(self):
        now = datetime(2026, 10, 22, 12, 0, 0)
        order = wat.Order(order_id="X", stage=wat.S_COOKING, mode="takeaway")
        eta = wat.estimate_eta(order, now)
        self.assertEqual(eta.confidence, "medium")
        self.assertIsNotNone(eta.remaining_min)

    def test_departure_wait_when_food_not_ready(self):
        now = datetime(2026, 10, 22, 12, 0, 0)
        eta = wat.ETA(remaining_min=20, confidence="high", basis="承诺时间", ready_at=now + timedelta(minutes=20))
        plan = wat.plan_departure(eta, travel_min=8, now=now)
        self.assertEqual(plan.tone, "wait")
        self.assertEqual(plan.leave_at, now + timedelta(minutes=10))

    def test_departure_go_when_time_matches(self):
        now = datetime(2026, 10, 22, 12, 0, 0)
        eta = wat.ETA(remaining_min=8, confidence="high", basis="承诺时间", ready_at=now + timedelta(minutes=8))
        plan = wat.plan_departure(eta, travel_min=8, buffer_min=2, now=now)
        self.assertEqual(plan.tone, "go")

    def test_cancelled_is_final(self):
        order = wat.Order(order_id="X", stage=wat.S_CANCELLED)
        self.assertTrue(order.is_final)
        self.assertIsNone(wat.estimate_eta(order).remaining_min)


class PlacesTest(unittest.TestCase):
    def test_distance_km_to_meters(self):
        store = plc.normalize_store({"storeName": "A店", "storeCode": "1", "distance": "1.2km"})
        self.assertAlmostEqual(store.distance_m, 1200)

    def test_service_normalization(self):
        services = plc.normalize_services(["堂食", "外带", "得来速"])
        self.assertEqual(services, {"dinein", "takeaway", "drivethru"})

    def test_rank_prefers_service_then_distance(self):
        raw = [
            {"storeName": "近但没外带", "storeCode": "1", "distance": 100, "services": ["堂食"]},
            {"storeName": "远但可外带", "storeCode": "2", "distance": 900, "services": ["堂食", "外带"]},
        ]
        stores = [plc.normalize_store(item) for item in raw]
        ranked = plc.rank_stores(stores, need={"takeaway"})
        self.assertEqual(ranked[0].name, "远但可外带")

    def test_closed_store_filtered(self):
        raw = [
            {"storeName": "休息中门店", "storeCode": "1", "distance": 100, "status": "休息中"},
            {"storeName": "营业门店", "storeCode": "2", "distance": 500, "status": "营业中"},
        ]
        stores = [plc.normalize_store(item) for item in raw]
        ranked = plc.rank_stores(stores, need=set())
        self.assertEqual([s.name for s in ranked], ["营业门店"])

    def test_travel_minutes(self):
        self.assertAlmostEqual(plc.travel_minutes(750, "walk"), 10.0)


if __name__ == "__main__":
    unittest.main()
