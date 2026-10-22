"""手机 ↔ 电脑同步：离线排队 → 回来补传，重传不能把数据算两遍。

手机带出门会断网，背的单词先排进浏览器队列，回到同一个 WiFi 再补传。
这个测试守的是三件事：
  1. 每条记录带 uid，重复补传只落一次（卡路里、单词数不会被重复累加）；
  2. 导出的快照能被另一台设备完整导入（meals / sessions / 单词进度）；
  3. 单词进度这类「聚合状态」合并时取信息量更大的一侧，而不是互相覆盖。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from time_to_mcd import sync as syncmod  # noqa: E402
from time_to_mcd.store import RecordStore  # noqa: E402


def _tmp_store() -> RecordStore:
    tmp = tempfile.mkdtemp(prefix="t2m-sync-")
    return RecordStore(Path(tmp) / "records.db")


def _play_op(uid, correct=True, minutes=3.0):
    return {
        "type": "play",
        "uid": uid,
        "ts": "2026-10-22T12:00:00",
        "payload": {
            "duration_min": minutes,
            "words": [{"en": "abandon", "correct": correct}, {"en": "benefit", "correct": True}],
        },
    }


def _meal_op(uid, kcal=520.0):
    return {
        "type": "meal",
        "uid": uid,
        "ts": "2026-10-22T12:30:00",
        "payload": {
            "meal_type": "lunch",
            "store": "麦当劳示例餐厅（脱敏）",
            "items": [{"name": "本餐", "qty": 1, "kcal": kcal, "protein": 28, "fat": 22, "carb": 55, "sodium": 800}],
        },
    }


def _commute_op(uid, kcal=45.0):
    return {
        "type": "commute",
        "uid": uid,
        "ts": "2026-10-22T12:10:00",
        "payload": {"mode": "walk", "distance_m": 900, "minutes": 12, "kcal": kcal, "store": "麦当劳示例餐厅（脱敏）"},
    }


class SyncOpsTest(unittest.TestCase):
    def test_apply_ops_counts(self):
        store = _tmp_store()
        res = syncmod.apply_ops(
            store,
            [_play_op("p1"), _meal_op("m1"), _commute_op("c1")],
            device="phone",
        )
        self.assertEqual(res["applied"], 3)
        self.assertEqual(res["skipped"], 0)
        self.assertEqual(res["errors"], [])
        stats = store.stats()
        self.assertEqual(stats["meals_total"], 1)
        self.assertEqual(stats["words_studied"], 2)
        self.assertEqual(stats["trips"], 1)
        self.assertEqual(store.get_meta("last_device"), "phone")

    def test_retry_is_idempotent(self):
        """断网重试 / 补传两次，只算一次。"""
        store = _tmp_store()
        syncmod.apply_ops(store, [_play_op("p1"), _meal_op("m1"), _commute_op("c1")], device="phone")
        again = syncmod.apply_ops(store, [_play_op("p1"), _meal_op("m1"), _commute_op("c1")], device="phone")
        self.assertEqual(again["applied"], 0)
        self.assertEqual(again["skipped"], 3)
        stats = store.stats()
        self.assertEqual(stats["meals_total"], 1)
        self.assertEqual(stats["trips"], 1)
        self.assertEqual(stats["kcal_in"], 520.0)
        # 单词进度：两个词各答对一次，重传后不能变成 2 次
        progress = store.word_progress()
        self.assertEqual(progress["abandon"]["correct"], 1)
        self.assertEqual(progress["benefit"]["correct"], 1)

    def test_wait_session_goes_to_focus_minutes(self):
        store = _tmp_store()
        syncmod.apply_ops(
            store,
            [{"type": "wait", "uid": "w1", "ts": "2026-10-22T12:20:00", "payload": {"minutes": 8.5, "ref_id": "order-1"}}],
            device="phone",
        )
        stats = store.stats()
        self.assertAlmostEqual(stats["focus_wait_minutes"], 8.5, places=1)

    def test_unknown_type_is_reported_not_crashing(self):
        store = _tmp_store()
        res = syncmod.apply_ops(store, [{"type": "nope", "uid": "x1", "payload": {}}], device="phone")
        self.assertEqual(len(res["errors"]), 1)
        self.assertIn("未知", res["errors"][0]["error"])

    def test_profile_and_anchor(self):
        store = _tmp_store()
        syncmod.apply_ops(
            store,
            [
                {"type": "profile", "uid": "pr1", "payload": {"height_cm": 175, "weight_kg": 68, "age": 22, "sex": "male", "activity": "light", "goal": "lose"}},
                {"type": "anchor", "uid": "a1", "payload": {"name": "示例汉堡", "category": "burger", "price": 21.0, "kcal": 520, "protein": 26, "fat": 24, "carb": 48, "sodium": 900}},
            ],
            device="phone",
        )
        self.assertIsNotNone(store.get_profile())
        self.assertEqual(len(store.food_anchors()), 1)


class SnapshotTest(unittest.TestCase):
    def test_export_then_import_roundtrip(self):
        phone = _tmp_store()
        syncmod.apply_ops(phone, [_play_op("p1"), _meal_op("m1"), _commute_op("c1")], device="phone")

        snap = phone.sync_snapshot()

        pc = _tmp_store()
        merged = pc.merge_snapshot(snap)
        self.assertEqual(merged["meals"], 1)
        self.assertEqual(merged["commutes"], 1)
        self.assertEqual(merged["sessions"], 1)
        self.assertEqual(merged["word_progress"], 2)

        stats = pc.stats()
        self.assertEqual(stats["meals_total"], 1)
        self.assertEqual(stats["words_studied"], 2)
        self.assertEqual(stats["trips"], 1)

    def test_import_twice_does_not_duplicate(self):
        phone = _tmp_store()
        syncmod.apply_ops(phone, [_play_op("p1"), _meal_op("m1")], device="phone")
        snap = phone.sync_snapshot()

        pc = _tmp_store()
        pc.merge_snapshot(snap)
        second = pc.merge_snapshot(snap)   # 手滑导入两次
        self.assertEqual(second["meals"], 0)
        self.assertEqual(second["sessions"], 0)
        self.assertEqual(pc.stats()["meals_total"], 1)

    def test_since_filters_old_records(self):
        store = _tmp_store()
        syncmod.apply_ops(store, [_meal_op("m1"), _commute_op("c1")], device="phone")
        full = store.sync_snapshot()
        later = store.sync_snapshot(since="2099-01-01")
        self.assertEqual(full["counts"]["meals"], 1)
        self.assertEqual(later["counts"]["meals"], 0)
        # 单词进度是聚合状态，永远全量，不参与增量
        self.assertEqual(full["counts"]["word_progress"], later["counts"]["word_progress"])

    def test_word_progress_merge_takes_the_bigger_side(self):
        """两台设备各背了一些词，合并后两边都要留下（取 max，不是覆盖）。"""
        a = _tmp_store()
        a.record_word("abandon", True)
        a.record_word("abandon", True)
        a.record_word("benefit", True)

        b = _tmp_store()
        b.record_word("abandon", True)
        b.record_word("cruise", True)

        merged = a.merge_snapshot(b.sync_snapshot())
        self.assertGreaterEqual(merged["word_progress"], 1)
        progress = a.word_progress()
        self.assertEqual(progress["abandon"]["correct"], 2)   # A 背了两次，不能被 B 的一次覆盖
        self.assertEqual(progress["benefit"]["correct"], 1)
        self.assertEqual(progress["cruise"]["correct"], 1)


class UidTest(unittest.TestCase):
    def test_uid_unique_per_device(self):
        a = syncmod.new_uid("phone")
        b = syncmod.new_uid("phone")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("phone-"))

    def test_log_meal_with_same_uid_only_once(self):
        store = _tmp_store()
        first = store.log_meal(items=[], kcal=100, protein=0, fat=0, carb=0, sodium=0, uid="same-uid")
        second = store.log_meal(items=[], kcal=100, protein=0, fat=0, carb=0, sodium=0, uid="same-uid")
        self.assertNotEqual(first, 0)
        self.assertEqual(second, 0)
        self.assertEqual(store.stats()["meals_total"], 1)

    def test_empty_uid_never_conflicts(self):
        """老数据没有 uid，不能因为唯一索引而写不进去。"""
        store = _tmp_store()
        store.log_meal(items=[], kcal=100, protein=0, fat=0, carb=0, sodium=0)
        store.log_meal(items=[], kcal=120, protein=0, fat=0, carb=0, sodium=0)
        self.assertEqual(store.stats()["meals_total"], 2)


if __name__ == "__main__":
    unittest.main()
