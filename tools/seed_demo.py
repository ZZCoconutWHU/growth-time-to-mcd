#!/usr/bin/env python
"""重建 data/demo.db：一份跨 10 天、指标齐全的演示数据。

用途：评审 / 演示时用 `python run.py web --demo` 打开，即可看到"满血"看板——
7 日柱状图排满、9 枚成就全部点亮、等级与经验条在线，背单词屏还能看到订单实时状态。
不需要真实 MCP Token，也**不污染** records.db 里属于用户自己的真实记录。

数据全部 backdate 到最近 10 天，因此 7 日图有内容、连续天数与累计指标也都够看。

幂等：每次运行都会删除并重建 demo.db。
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from time_to_mcd.coach import meal_plan  # noqa: E402
from time_to_mcd.store import Profile, RecordStore  # noqa: E402

DEMO_DB = ROOT / "data" / "demo.db"
WORDBANK = ROOT / "data" / "wordbank.json"
DAYS = 10  # 覆盖 10 天（7 日图只取最近 7 天，更早的计入累计）

# 演示库刻意使用虚构店名：截图/投屏时不会泄露用户真实常去的门店
STORES = [
    "麦当劳(示例·中心广场店)",
    "麦当劳(示例·地铁口店)",
    "麦当劳(示例·步行街店)",
]
# 每餐的"点单内容"（时间线只展示 名称×数量，金额不进展示）
MEAL_ITEMS = {
    "breakfast": [("猪柳蛋麦满分", 1), ("热豆浆", 1)],
    "lunch": [("巨无霸", 1), ("中薯条", 1)],
    "dinner": [("麦辣鸡腿堡", 1), ("玉米杯", 1)],
}


def _ts(day: date, hh: int, mm: int) -> str:
    return datetime(day.year, day.month, day.day, hh, mm, 0).isoformat(timespec="seconds")


def seed() -> dict:
    if DEMO_DB.exists():
        DEMO_DB.unlink()

    store = RecordStore(DEMO_DB)

    # ---- 档案（虚构，刻意区别于任何真实用户数据）----
    profile = Profile(height_cm=170, weight_kg=62, age=26, sex="male", activity="light", goal="lose")
    store.save_profile(profile)
    plan = meal_plan(profile)
    meals_t = plan["meals"]

    # ---- 词库 ----
    words = json.loads(WORDBANK.read_text(encoding="utf-8")).get("words", [])
    store.seed_wordbank(words)

    today = date.today()
    days = [today - timedelta(days=offset) for offset in range(DAYS - 1, -1, -1)]

    rnd = random.Random(42)
    conn = sqlite3.connect(DEMO_DB)
    try:
        # ================= 单词进度：110 词，其中 34 个进长期记忆 =================
        bank = words[:]
        rnd.shuffle(bank)
        studied = bank[:110]
        for idx, w in enumerate(studied):
            mastered = idx < 34
            if mastered:
                reps = rnd.choice([3, 3, 4, 5])
                correct = rnd.randint(4, 8)
                wrong = rnd.randint(0, 1)
                ease = round(rnd.uniform(2.6, 2.9), 2)
                interval = rnd.choice([7, 10, 15, 21])
                due = (today + timedelta(days=interval)).isoformat()
            else:
                reps = rnd.choice([1, 2])
                correct = rnd.randint(1, 3)
                wrong = rnd.randint(0, 2)
                ease = round(rnd.uniform(2.2, 2.6), 2)
                interval = rnd.choice([1, 2, 3])
                due = (today + timedelta(days=rnd.randint(-2, 2))).isoformat()
            last = _ts(rnd.choice(days), rnd.randint(7, 22), rnd.randint(0, 59))
            conn.execute(
                """INSERT INTO word_progress (word, reps, ease, interval_d, due_date, correct, wrong, last_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (w["en"], reps, ease, interval, due, correct, wrong, last),
            )

        # ================= 每天：3 餐 + 1 趟通勤 + 1 次背词 =================
        meal_plan_types = [("breakfast", 8, 10), ("lunch", 12, 20), ("dinner", 18, 40)]
        for di, day in enumerate(days):
            # --- 三顿饭 ---
            for mi, (mtype, hh, mm) in enumerate(meal_plan_types):
                t = meals_t[mtype]
                # 大部分落在 ±8%→达标；最后一两天故意有 1-2 餐偏离，制造"未达标"对照
                off = (di >= DAYS - 2) and (mi == 1)
                factor = 1.28 if off else rnd.uniform(0.93, 1.06)
                kcal = round(t["kcal"] * factor, 1)
                protein = round(t["protein"] * (1.05 if off else rnd.uniform(0.95, 1.05)), 1)
                fat = round(t["fat"] * factor, 1)
                carb = round(t["carb"] * factor, 1)
                sodium = round(rnd.uniform(600, 1100), 1)
                target_kcal = float(t["kcal"])
                on_target = abs(kcal - target_kcal) <= target_kcal * 0.15
                items = [
                    {"name": n, "qty": q, "kcal": round(kcal / len(MEAL_ITEMS[mtype]), 1),
                     "protein": round(protein / len(MEAL_ITEMS[mtype]), 1),
                     "fat": round(fat / len(MEAL_ITEMS[mtype]), 1),
                     "carb": round(carb / len(MEAL_ITEMS[mtype]), 1), "sodium": round(sodium / len(MEAL_ITEMS[mtype]), 1)}
                    for n, q in MEAL_ITEMS[mtype]
                ]
                conn.execute(
                    """INSERT INTO meals (ts, store, meal_type, items_json, kcal, protein, fat, carb, sodium, target_kcal, on_target)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (_ts(day, hh, mm), STORES[di % len(STORES)], mtype,
                     json.dumps(items, ensure_ascii=False), kcal, protein, fat, carb, sodium,
                     target_kcal, 1 if on_target else 0),
                )

            # --- 一趟通勤（步行/骑行交替） ---
            mode = "walk" if di % 2 == 0 else "bike"
            distance_m = float(rnd.choice([2100, 2300, 2450, 2600, 2800]))
            km = distance_m / 1000
            if mode == "walk":
                minutes = round(km / 4.5 * 60, 1)
                kcal_c = round(km * 55, 1)
            else:
                minutes = round(km / 14 * 60, 1)
                kcal_c = round(km * 30, 1)
            conn.execute(
                "INSERT INTO commutes (ts, store, mode, distance_m, minutes, kcal) VALUES (?, ?, ?, ?, ?, ?)",
                (_ts(day, 8, 45), STORES[di % len(STORES)], mode, distance_m, minutes, kcal_c),
            )

            # --- 一次等餐背词 ---
            n_words = rnd.randint(10, 16)
            correct = n_words - rnd.randint(0, 2)
            duration = round(rnd.uniform(2.5, 4.5), 1)
            conn.execute(
                """INSERT INTO sessions (ts, kind, duration_min, words, correct, kcal_burned, meta_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (_ts(day, 12, 35), "words", duration, n_words, correct, 0.0,
                 json.dumps({"source": "demo", "store": STORES[di % len(STORES)]}, ensure_ascii=False)),
            )

        conn.commit()
    finally:
        conn.close()

    # 触发成就写入（导出时会再算一次，但这里先落库更直观）
    store.achievements()
    return store.stats()


def main() -> int:
    stats = seed()
    db = sqlite3.connect(f"file:{DEMO_DB}?mode=ro", uri=True)
    try:
        n_meals = db.execute("SELECT COUNT(*) FROM meals").fetchone()[0]
        n_commutes = db.execute("SELECT COUNT(*) FROM commutes").fetchone()[0]
        n_sessions = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        n_words = db.execute("SELECT COUNT(*) FROM word_progress").fetchone()[0]
    finally:
        db.close()
    print(f"已重建演示库：{DEMO_DB}")
    print(f"  餐 {n_meals} / 通勤 {n_commutes} / 背词场次 {n_sessions} / 单词 {n_words}")
    print(
        f"  达标餐 {stats['meals_on_target']}/{stats['meals_total']} · "
        f"单词 {stats['words_studied']}（掌握 {stats['words_mastered']}）· "
        f"消耗 {stats['kcal_burned']} kcal · 里程 {stats['distance_km']} km · "
        f"蛋白 {stats['protein_in']} g · 连续 {stats['streak_days']} 天"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
