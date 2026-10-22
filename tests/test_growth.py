"""成长模块单测：预点餐教练、单词引擎、本地档案、地图解码。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from time_to_mcd import coach, geo, words as wds  # noqa: E402
from time_to_mcd.nutrition import Food  # noqa: E402
from time_to_mcd.store import Profile, RecordStore  # noqa: E402


class CoachTest(unittest.TestCase):
    def setUp(self):
        self.profile = Profile(height_cm=178, weight_kg=72, age=30, sex="male", activity="light", goal="lose")

    def test_bmr_mifflin(self):
        self.assertAlmostEqual(coach.bmr(self.profile), 1687.5, places=1)

    def test_tdee_uses_activity_factor(self):
        self.assertAlmostEqual(coach.tdee(self.profile), 1687.5 * 1.375, places=1)

    def test_lunch_targets(self):
        targets = coach.compute_targets(self.profile, "lunch")
        self.assertGreater(targets.kcal, 600)
        self.assertLess(targets.kcal, 800)
        self.assertGreater(targets.protein, 40)

    def test_bmi_bands(self):
        self.assertEqual(coach.bmi_band(17.0), "偏瘦")
        self.assertEqual(coach.bmi_band(22.0), "正常")
        self.assertEqual(coach.bmi_band(26.0), "超重")
        self.assertEqual(coach.bmi_band(30.0), "肥胖")

    def test_classify(self):
        self.assertEqual(coach.classify(Food("巨无霸", category="汉堡")), "burger")
        self.assertEqual(coach.classify(Food("中薯条", category="小食")), "side")
        self.assertEqual(coach.classify(Food("中杯无糖可乐", category="饮料")), "drink")
        self.assertEqual(coach.classify(Food("甜筒", category="甜品")), "dessert")

    def test_protein_weighted_above_calorie_only(self):
        targets = coach.Targets(kcal=700, protein=45, fat=23, carb=75)
        balanced = coach.Combo(
            items=[Food("A")], kcal=690, protein=44, fat=20, carb=70, sodium=800
        )
        lean_but_low_protein = coach.Combo(
            items=[Food("B")], kcal=700, protein=26, fat=18, carb=100, sodium=700
        )
        self.assertGreater(
            coach.score_combo(balanced, targets),
            coach.score_combo(lean_but_low_protein, targets),
        )

    def test_recommend_returns_ranked_combos(self):
        catalog = [
            Food("板烧鸡腿堡", energy=400, protein=27, fat=14, carb=41, sodium=820),
            Food("巨无霸", energy=550, protein=25, fat=30, carb=42, sodium=1000),
            Food("玉米杯", energy=90, protein=3, fat=1, carb=19, sodium=0),
            Food("中薯条", energy=340, protein=4, fat=16, carb=44, sodium=250),
            Food("中杯无糖可乐", energy=0, protein=0, fat=0, carb=0, sodium=10),
            Food("中杯可口可乐", energy=200, protein=0, fat=0, carb=50, sodium=15),
        ]
        targets = coach.Targets(kcal=650, protein=40, fat=22, carb=70)
        combos = coach.recommend(catalog, targets, limit=2)
        self.assertTrue(combos)
        self.assertGreaterEqual(combos[0].score, combos[-1].score)


class WordEngineTest(unittest.TestCase):
    def setUp(self):
        self.bank = wds.load_wordbank()

    def test_bank_loaded(self):
        self.assertGreater(len(self.bank), 100)
        self.assertTrue(all(word.en and word.zh for word in self.bank))

    def test_plan_count(self):
        self.assertEqual(wds.plan_count(7), 42)
        self.assertEqual(wds.plan_count(0.1), 4)  # 至少 4 题

    def test_due_words_come_first(self):
        target = self.bank[5]
        picked = wds.select_words(self.bank, 3, due=[target.en])
        self.assertEqual(picked[0].en, target.en)

    def test_questions_have_four_unique_options(self):
        sample = self.bank[:8]
        questions = wds.build_questions(sample, self.bank)
        for question in questions:
            self.assertEqual(len(question.options), 4)
            self.assertEqual(len(set(question.options)), 4)
            self.assertEqual(question.options[question.answer], question.word.zh if question.direction == "en2zh" else question.word.en)

    def test_parse_choice(self):
        self.assertEqual(wds.parse_choice("B"), 1)
        self.assertEqual(wds.parse_choice("3"), 2)
        self.assertIsNone(wds.parse_choice("z"))
        self.assertIsNone(wds.parse_choice("9"))


class RecordStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RecordStore(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_profile_roundtrip(self):
        self.assertIsNone(self.store.get_profile())
        self.store.save_profile(Profile(178, 72, 30))
        profile = self.store.get_profile()
        self.assertEqual(profile.weight_kg, 72)

    def test_spaced_repetition_grows_interval(self):
        first = self.store.record_word("abandon", True)
        self.assertEqual(first.interval_d, 1)
        second = self.store.record_word("abandon", True)
        self.assertEqual(second.interval_d, 3)
        third = self.store.record_word("abandon", True)
        self.assertGreaterEqual(third.interval_d, 7)
        self.assertTrue(third.mastered)

        failed = self.store.record_word("abandon", False)
        self.assertEqual(failed.interval_d, 1)
        self.assertFalse(failed.mastered)

    def test_stats_and_badges(self):
        self.store.log_meal([{"name": "板烧鸡腿堡"}], 400, 27, 14, 41, 820, target_kcal=420, on_target=True)
        self.store.log_commute("A店", "walk", 800, 10, 35)
        self.store.record_word("abandon", True)
        stats = self.store.stats()
        self.assertEqual(stats["meals_total"], 1)
        self.assertEqual(stats["meals_on_target"], 1)
        self.assertEqual(stats["distance_km"], 0.8)
        badges = {badge["code"]: badge["unlocked"] for badge in self.store.achievements(stats)}
        self.assertTrue(badges["first_meal"])
        self.assertFalse(badges["words_100"])

    def test_streak_counts_today(self):
        self.store.log_session("words", duration_min=5, words=10, correct=8)
        self.assertEqual(self.store.stats()["streak_days"], 1)


class GeoTest(unittest.TestCase):
    def test_decode_polyline(self):
        points = geo.decode_polyline([31223720, 121445310, 1000, 2000])
        self.assertEqual(len(points), 2)
        self.assertAlmostEqual(points[0][0], 31.223720, places=5)
        self.assertAlmostEqual(points[1][0], 31.224720, places=5)

    def test_decode_rejects_garbage(self):
        self.assertEqual(geo.decode_polyline([1, 2, 3]), [])
        self.assertEqual(geo.decode_polyline([]), [])

    def test_haversine(self):
        distance = geo.haversine_m((31.2237, 121.4453), (31.2243, 121.4471))
        self.assertGreater(distance, 100)
        self.assertLess(distance, 400)

    def test_estimate_route_adds_detour(self):
        route = geo.estimate_route(1000, "walk")
        self.assertAlmostEqual(route.distance_m, 1250.0)
        self.assertAlmostEqual(route.minutes, 1250 / 75, places=1)

    def test_metro_calories(self):
        route = geo.estimate_route(750, "walk")
        # 750*1.25=937.5m → 12.5min；3.5 MET × 3.5 × 70 / 200 × 12.5
        self.assertGreater(route.calories(70), 40)
        self.assertLess(route.calories(70), 60)

    def test_no_key_means_no_service_call(self):
        client = geo.TencentGeo("")
        self.assertFalse(client.available)
        self.assertIsNone(client.route((31.2, 121.4), (31.3, 121.5)))


if __name__ == "__main__":
    unittest.main()
