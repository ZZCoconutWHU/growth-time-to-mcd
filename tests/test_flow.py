"""端到端链路：mock 模式下 通勤 → 预点餐 → 候餐 → 单词 → 看板 全流程。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from time_to_mcd import coach, words as wds  # noqa: E402
from time_to_mcd.geo import TencentGeo  # noqa: E402
from time_to_mcd.mock import MockBox  # noqa: E402
from time_to_mcd.report import render_journey  # noqa: E402
from time_to_mcd.service import CompassService  # noqa: E402
from time_to_mcd.store import RecordStore  # noqa: E402


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "records.db"
        self.service = CompassService(
            MockBox(),
            records=RecordStore(self.db_path),
            geo=TencentGeo(""),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_stores_sorted_and_open_only(self):
        stores = self.service.find_stores("上海静安嘉里中心", need={"takeaway"})
        self.assertTrue(stores)
        self.assertTrue(all(s.is_open for s in stores))
        distances = [s.distance_m for s in stores]
        self.assertEqual(distances, sorted(distances))

    def test_catalog_loaded(self):
        catalog = self.service.catalog()
        self.assertGreaterEqual(len(catalog), 10)
        self.assertIn("巨无霸", [food.name for food in catalog])

    def test_recommend_hits_target(self):
        profile = self.service.save_profile(178, 72, 30, goal="lose")
        targets, combos, size = self.service.recommend(profile, meal="lunch", limit=3)
        self.assertGreaterEqual(size, 10)
        self.assertTrue(combos)
        best = combos[0]
        self.assertLessEqual(abs(best.kcal - targets.kcal), targets.kcal * 0.3)
        self.assertTrue(best.items)

    def test_commute_falls_back_to_estimate(self):
        store = self.service.find_stores("上海静安嘉里中心")[0]
        route = self.service.commute(store, mode="walk")
        self.assertEqual(route.source, "estimate")
        self.assertGreater(route.distance_m, store.distance_m)
        self.assertGreater(route.calories(72), 0)

    def test_wait_state(self):
        order = self.service.order_detail()
        self.assertIsNotNone(order)
        eta, plan = self.service.wait_state(order, travel_min=6)
        self.assertIsNotNone(eta.remaining_min)
        self.assertIn(plan.tone, {"go", "wait", "hurry"})

    def test_logging_updates_stats(self):
        profile = self.service.save_profile(178, 72, 30, goal="lose")
        targets, combos, _ = self.service.recommend(profile, meal="lunch")
        store = self.service.find_stores("上海静安嘉里中心")[0]
        route = self.service.commute(store, mode="walk")

        self.service.log_commute(store, route, profile.weight_kg)
        self.service.log_combo(combos[0], targets)

        stats = self.service.stats()
        self.assertEqual(stats["meals_total"], 1)
        self.assertEqual(stats["trips"], 1)
        self.assertGreater(stats["protein_in"], 0)
        self.assertGreater(stats["kcal_burned"], 0)

    def test_word_session_records_progress(self):
        bank = wds.load_wordbank()
        result = wds.play_session(
            self.service.db,
            minutes=1,
            bank=bank,
            answers=["1"] * 10,
            seed=3,
            tell=lambda _msg: None,
            ask=lambda _prompt: "1",
        )
        self.assertEqual(result.total, 6)
        stats = self.service.stats()
        self.assertEqual(stats["words_studied"], 6)
        # 答对后间隔排到明天，今天不应到期，但进度必须已落库
        self.assertEqual(len(self.service.db.word_progress()), 6)
        self.assertEqual(self.service.db.due_words(10), [])

    def test_journey_report_contains_all_sections(self):
        profile = self.service.save_profile(178, 72, 30, goal="lose")
        summary = coach.profile_summary(profile)
        targets, combos, _ = self.service.recommend(profile, meal="lunch")
        store = self.service.find_stores("上海静安嘉里中心")[0]
        route = self.service.commute(store, mode="walk")
        order = self.service.order_detail()
        eta, plan = self.service.wait_state(order, travel_min=route.minutes)

        text = render_journey(
            address="上海静安嘉里中心",
            profile=profile,
            summary=summary,
            targets=targets,
            store=store,
            route=route,
            combos=combos,
            order=order,
            eta=eta,
            plan=plan,
            stats=self.service.stats(),
            word_count=wds.plan_count(eta.remaining_min or 0),
        )
        for marker in ("一句话", "在路上", "这一餐", "还要等多久", "累计"):
            self.assertIn(marker, text)


if __name__ == "__main__":
    unittest.main()
