#!/usr/bin/env python
"""把 SQLite 成长记录导出为前端可直接读取的 stats.js / stats.json。

用法：
    python tools/export_web.py                    # 自动挑选记录最多的库
    python tools/export_web.py --db data/demo.db  # 指定库
    python tools/export_web.py --out web          # 指定输出目录（默认 web）

为什么同时产出 .js 和 .json：
    .js   -> 直接用 file:// 双击打开网页也能读到数据（不受 CORS 限制）
    .json -> 将来挂到 HTTP 服务上时，前端优先 fetch 它拿最新数据
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 等级体系
# 经验值来源：达标餐、单词、通勤消耗、里程、专注时长
LEVELS: list[tuple[int, str, str]] = [
    (0, "见习麦友", "刚拿到第一张取餐小票"),
    (60, "麦苗新芽", "开始把等餐时间攒起来"),
    (180, "麦香学徒", "单词和步数都在慢慢长"),
    (400, "汉堡侍从", "已经能算出这一餐的蛋白"),
    (750, "薯条骑士", "通勤路上也能背完一组词"),
    (1300, "麦麦达人", "热量、蛋白、里程三线并进"),
    (2200, "金拱门主厨", "把等待过成了日常仪式"),
    (3600, "麦麦传奇", "这条路你已经走得比菜单还熟"),
]

BADGE_GOALS: list[tuple[str, str, str, str, float]] = [
    # code, 名称, 说明, 取值字段, 目标值
    ("first_meal", "第一餐达标", "有一餐落在热量与蛋白目标区间内", "meals_on_target", 1),
    ("meals_10", "十餐知己", "累计 10 餐达标", "meals_on_target", 10),
    ("words_50", "五十词", "累计学习 50 个单词", "words_studied", 50),
    ("words_100", "百词斩", "累计学习 100 个单词", "words_studied", 100),
    ("mastered_30", "三十而熟", "30 个单词进入长期记忆", "words_mastered", 30),
    ("burn_500", "燃烧五百", "通勤累计消耗 500 kcal", "kcal_burned", 500),
    ("walk_10km", "十公里", "步行 / 骑行累计 10 km", "distance_km", 10),
    ("streak_3", "三日不断", "连续 3 天有记录", "streak_days", 3),
    ("protein_1kg", "蛋白一公斤", "累计摄入蛋白质 1000 g", "protein_in", 1000),
]

# 成就图标（与前端 symbol id 对应）
BADGE_ICON = {
    "first_meal": "burger",
    "meals_10": "burger",
    "words_50": "book",
    "words_100": "book",
    "mastered_30": "brain",
    "burn_500": "flame",
    "walk_10km": "shoe",
    "streak_3": "clock",
    "protein_1kg": "meat",
}


def pick_db(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env = __import__("os").environ.get("T2M_DB")
    if env:
        return Path(env)
    candidates = [ROOT / "data" / "records.db", ROOT / "data" / "demo.db"]
    existing = [p for p in candidates if p.exists()]
    if not existing:
        return candidates[0]
    # 选记录最多的那个，避免导到空库
    def size(p: Path) -> int:
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            n = conn.execute(
                "SELECT (SELECT COUNT(*) FROM meals)+(SELECT COUNT(*) FROM commutes)"
                "+(SELECT COUNT(*) FROM word_progress)"
            ).fetchone()[0]
            conn.close()
            return n
        except sqlite3.Error:
            return 0

    return max(existing, key=size)


def _rows(conn: sqlite3.Connection, sql: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql)]


def collect(db_path: Path) -> dict:
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}\n先跑一次 `python run.py --mock journey ...` 生成记录。")

    sys.path.insert(0, str(ROOT / "src"))
    from time_to_mcd.store import RecordStore  # noqa: E402

    store = RecordStore(db_path)
    stats = store.stats()
    unlocked = {b["code"]: b["unlocked_at"] for b in store.achievements(stats)}

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    meals = _rows(
        conn,
        "SELECT ts, store, meal_type, items_json, kcal, protein, fat, carb, sodium,"
        " target_kcal, on_target FROM meals ORDER BY ts DESC LIMIT 8",
    )
    commutes = _rows(
        conn,
        "SELECT ts, store, mode, distance_m, minutes, kcal FROM commutes ORDER BY ts DESC LIMIT 8",
    )
    sessions = _rows(
        conn,
        "SELECT ts, kind, duration_min, words, correct, kcal_burned FROM sessions ORDER BY ts DESC LIMIT 8",
    )
    profile = _rows(conn, "SELECT * FROM profile ORDER BY id DESC LIMIT 1")
    profile = profile[0] if profile else {}
    # 附上 BMI 与分餐目标，前端无需再算（公式来自 coach.meal_plan 单一真源）
    if profile:
        try:
            sys.path.insert(0, str(ROOT / "src"))
            from time_to_mcd.store import Profile as P  # noqa: E402
            from time_to_mcd.coach import meal_plan  # noqa: E402

            p = P(
                height_cm=profile["height_cm"],
                weight_kg=profile["weight_kg"],
                age=profile["age"],
                sex=profile.get("sex", "male"),
                activity=profile.get("activity", "light"),
                goal=profile.get("goal", "maintain"),
            )
            plan = meal_plan(p)
            profile["bmi"] = plan["summary"]["bmi"]
            profile["bmi_band"] = plan["summary"]["bmi_band"]
            profile["tdee"] = plan["summary"]["tdee"]
            profile["targets"] = {m: v for m, v in plan["meals"].items()}
        except Exception:
            pass

    # 最近 7 天趋势
    today = date.today()
    days = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    daily = []
    for day in days:
        d = day  # SQLite substr(ts,1,10)
        m = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(on_target),0) t, COALESCE(SUM(kcal),0) k,"
            " COALESCE(SUM(protein),0) p FROM meals WHERE substr(ts,1,10)=?",
            (d,),
        ).fetchone()
        c = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(distance_m),0) m, COALESCE(SUM(kcal),0) k,"
            " COALESCE(SUM(minutes),0) min FROM commutes WHERE substr(ts,1,10)=?",
            (d,),
        ).fetchone()
        s = conn.execute(
            "SELECT COALESCE(SUM(words),0) w, COALESCE(SUM(duration_min),0) min"
            " FROM sessions WHERE substr(ts,1,10)=?",
            (d,),
        ).fetchone()
        daily.append(
            {
                "date": d,
                "meals": m[0],
                "on_target": m[1],
                "meal_kcal": round(m[2], 1),
                "protein": round(m[3], 1),
                "trips": c[0],
                "distance_m": round(c[1], 1),
                "burn_kcal": round(c[2], 1),
                "commute_min": round(c[3], 1),
                "words": s[0],
                "focus_min": round(s[1], 1),
            }
        )
    conn.close()

    # ------------------------------------------------------------ 经验与等级
    xp = (
        stats["meals_on_target"] * 25
        + stats["words_studied"] * 3
        + stats["words_mastered"] * 10
        + round(stats["kcal_burned"])
        + round(stats["distance_km"] * 10)
        + stats["trips"] * 5
        + stats["sessions"] * 10
    )
    level_idx = 0
    for i, (need, _, _) in enumerate(LEVELS):
        if xp >= need:
            level_idx = i
    need_cur = LEVELS[level_idx][0]
    title_cur = LEVELS[level_idx][1]
    motto_cur = LEVELS[level_idx][2]
    if level_idx + 1 < len(LEVELS):
        need_next = LEVELS[level_idx + 1][0]
        next_title = LEVELS[level_idx + 1][1]
        ratio = (xp - need_cur) / max(1, need_next - need_cur)
    else:
        need_next, next_title, ratio = need_cur, "已满级", 1.0

    # ------------------------------------------------------------ 成就进度
    badges = []
    for code, name, desc, field, goal in BADGE_GOALS:
        cur = float(stats.get(field, 0) or 0)
        badges.append(
            {
                "code": code,
                "name": name,
                "desc": desc,
                "icon": BADGE_ICON.get(code, "star"),
                "unlocked": bool(unlocked.get(code)),
                "unlocked_at": unlocked.get(code),
                "current": round(cur, 1),
                "goal": goal,
                "percent": min(100.0, cur / goal * 100),
            }
        )

    # ------------------------------------------------------------ 派生趣味指标
    words_per_min = (
        round(stats["words_studied"] / stats["focus_minutes"], 1) if stats["focus_minutes"] else 0.0
    )
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db": str(db_path),
        "profile": {k: v for k, v in profile.items() if k not in ("id",)},
        "stats": stats,
        "level": {
            "xp": xp,
            "title": title_cur,
            "motto": motto_cur,
            "index": level_idx,
            "need": need_cur,
            "next_title": next_title,
            "next_need": need_next,
            "percent": round(min(100.0, max(0.0, ratio * 100)), 1),
            "to_next": max(0, need_next - xp),
        },
        "badges": badges,
        "daily": daily,
        "recent": {
            "meals": meals,
            "commutes": commutes,
            "sessions": sessions,
        },
        "derived": {
            "words_per_min": words_per_min,
            "egg_equivalent": round(stats["protein_in"] / 6.0, 1),  # 一个鸡蛋 ≈ 6g 蛋白
            "on_target_rate": round(stats["meals_on_target"] / stats["meals_total"] * 100, 1)
            if stats["meals_total"]
            else 0.0,
            "avg_trip_kcal": round(stats["kcal_burned"] / stats["trips"], 1) if stats["trips"] else 0.0,
            "burger_equivalent": round(stats["kcal_burned"] / 550.0, 2) if stats["kcal_burned"] else 0.0,
        },
    }
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="导出成长数据给前端页面")
    ap.add_argument("--db", help="SQLite 路径，默认自动挑选")
    ap.add_argument("--out", default="web", help="输出目录，默认 web")
    args = ap.parse_args()

    db_path = pick_db(args.db)
    data = collect(db_path)
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "stats.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # stats.js 让 file:// 直接打开也能读到数据
    (out_dir / "stats.js").write_text(
        "window.T2M_DATA = " + json.dumps(data, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )

    s = data["stats"]
    print(f"数据库 : {db_path}")
    print(f"产出   : {out_dir / 'stats.js'} / {out_dir / 'stats.json'}")
    print(
        f"快照   : 达标餐 {s['meals_on_target']}/{s['meals_total']} · "
        f"单词 {s['words_studied']} · 消耗 {s['kcal_burned']} kcal · "
        f"蛋白 {s['protein_in']} g · 等级「{data['level']['title']}」"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
