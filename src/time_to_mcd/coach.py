"""预点餐教练：用 BMI / 活动量推每日能量，再折算到这一餐的四大目标。

公式全部为公开标准，不引入第三方库：
  - 基础代谢：Mifflin-St Jeor
  - 活动系数：Harris-Benedict 常用取值
  - 蛋白质：按体重 g/kg（减脂与增肌取高值）
  - 脂肪：占总能量 30%；碳水：剩余能量
推荐候选来自麦当劳 MCP 的真实菜单，不内置任何餐品数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .nutrition import Food
from .store import Profile

ACTIVITY_FACTORS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
}

GOAL_ADJUST = {"lose": -0.15, "maintain": 0.0, "gain": 0.10}
PROTEIN_PER_KG = {"lose": 1.8, "maintain": 1.5, "gain": 1.8}

MEAL_SHARE = {"breakfast": 0.25, "lunch": 0.35, "dinner": 0.30, "snack": 0.10}
MEAL_LABEL = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "snack": "加餐"}

# 按当前时间落到哪个餐次：起点时刻（时, 分）→ 餐次，从早到晚排列。
# 10:30 是麦当劳早餐切换为正餐的时点，17:00 起算晚餐、21:30 后归为夜宵（加餐）。
MEAL_WINDOWS: Tuple[Tuple[int, int, str], ...] = (
    (5, 0, "breakfast"),
    (10, 30, "lunch"),
    (14, 30, "snack"),
    (17, 0, "dinner"),
    (21, 30, "snack"),
)


def meal_of_time(now: Optional[Any] = None) -> str:
    """按当前时间推荐餐次，用于进入页面时的默认值。

    时间来自麦当劳 MCP 的 now-time-info（服务端时间），拿不到才用本机时间。
    用户手动切换后以用户选择为准，不再自动改。
    """
    from datetime import datetime

    moment = now if hasattr(now, "hour") else datetime.now()
    minutes = moment.hour * 60 + moment.minute
    picked = MEAL_WINDOWS[-1][2]  # 凌晨（< 05:00）算作前一晚的夜宵
    for hour, minute, meal in MEAL_WINDOWS:
        if minutes >= hour * 60 + minute:
            picked = meal
    return picked


def meal_windows(meal: str) -> List[str]:
    """某个餐次对应的时间段（可能不止一段，比如加餐有下午茶和夜宵）。"""
    out: List[str] = []
    for index, (hour, minute, name) in enumerate(MEAL_WINDOWS):
        if name != meal:
            continue
        # 每段的结束就是下一段的起点；最后一段接到次日 05:00
        end = MEAL_WINDOWS[index + 1][:2] if index + 1 < len(MEAL_WINDOWS) else (5, 0)
        out.append(f"{hour:02d}:{minute:02d}–{end[0]:02d}:{end[1]:02d}")
    return out


def meal_window_text(meal: str) -> str:
    """把时间段说清楚，界面上标注自动推荐的依据。"""
    return "、".join(meal_windows(meal)) or "全天"

ACTIVITY_LABEL = {
    "sedentary": "久坐（很少运动）",
    "light": "轻度（每周 1-3 次）",
    "moderate": "中度（每周 3-5 次）",
    "active": "高强度（每周 6-7 次）",
}
GOAL_LABEL = {"lose": "减脂", "maintain": "维持", "gain": "增肌"}

# 中国居民膳食指南（2022）要点，仅作科普提示，不替代医嘱
GUIDELINE_NOTE = (
    "参考《中国居民膳食指南（2022）》：食物多样、合理搭配；"
    "餐次能量建议占比 早 25–30% / 午 30–40% / 晚 30–35%；"
    "蛋白质供能 10–15%、脂肪 20–30%、碳水 50–65%；"
    "每日蔬菜 300–500g、奶及奶制品 300ml、全谷物与优质蛋白适量。"
)


@dataclass
class Targets:
    kcal: float
    protein: float
    fat: float
    carb: float
    sodium: float = 900.0
    meal: str = "lunch"

    @property
    def label(self) -> str:
        return MEAL_LABEL.get(self.meal, self.meal)


@dataclass
class Combo:
    items: List[Food]
    kcal: float
    protein: float
    fat: float
    carb: float
    sodium: float
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)

    @property
    def names(self) -> str:
        return " + ".join(food.name for food in self.items)

    @property
    def fat_ratio(self) -> float:
        return self.fat * 9 / self.kcal if self.kcal else 0.0


def bmr(profile: Profile) -> float:
    base = 10 * profile.weight_kg + 6.25 * profile.height_cm - 5 * profile.age
    return base + (5 if profile.sex.lower().startswith("m") else -161)


def tdee(profile: Profile) -> float:
    return bmr(profile) * ACTIVITY_FACTORS.get(profile.activity, 1.375)


def compute_targets(profile: Profile, meal: str = "lunch") -> Targets:
    share = MEAL_SHARE.get(meal, 0.35)
    daily_kcal = tdee(profile) * (1 + GOAL_ADJUST.get(profile.goal, 0.0))
    kcal = daily_kcal * share

    protein_day = profile.weight_kg * PROTEIN_PER_KG.get(profile.goal, 1.5)
    protein = max(15.0, protein_day * share)
    fat = kcal * 0.30 / 9
    carb = max(0.0, (kcal - protein * 4 - fat * 9) / 4)
    return Targets(kcal=round(kcal), protein=round(protein, 1), fat=round(fat, 1), carb=round(carb, 1), meal=meal)


def bmi_band(value: float) -> str:
    if value < 18.5:
        return "偏瘦"
    if value < 24:
        return "正常"
    if value < 28:
        return "超重"
    return "肥胖"


# --------------------------------------------------------------------- 分类
_BURGER_HINTS = ("汉堡", "堡")
_SIDE_HINTS = ("薯", "鸡块", "鸡翅", "鸡腿", "沙拉", "玉米", "苹果", "派", "薯饼")
_DRINK_HINTS = ("可乐", "雪碧", "芬达", "汁", "咖啡", "拿铁", "茶", "奶昔", "水")
_DESSERT_HINTS = ("甜筒", "圣代", "麦旋风", "冰淇淋", "蛋糕")


def classify(food: Food) -> str:
    text = f"{food.category} {food.name}"
    if any(hint in text for hint in _DESSERT_HINTS):
        return "dessert"
    if any(hint in text for hint in _DRINK_HINTS):
        return "drink"
    if any(hint in text for hint in _BURGER_HINTS):
        return "burger"
    if any(hint in text for hint in _SIDE_HINTS):
        return "side"
    return "other"


def _totals(items: Sequence[Food]) -> Dict[str, float]:
    return {
        "kcal": sum(f.energy for f in items),
        "protein": sum(f.protein for f in items),
        "fat": sum(f.fat for f in items),
        "carb": sum(f.carb for f in items),
        "sodium": sum(f.sodium for f in items),
    }


def evaluate(combo: Combo, targets: Targets) -> Combo:
    """给组合打上「达标 / 缺口」标签，供报告直接展示。"""
    kcal_gap = combo.kcal - targets.kcal
    if abs(kcal_gap) <= targets.kcal * 0.15:
        combo.reasons.append(f"热量 {combo.kcal:.0f} kcal，落在目标 ±15% 内")
    elif kcal_gap > 0:
        combo.gaps.append(f"热量超目标 {kcal_gap:.0f} kcal")
    else:
        combo.gaps.append(f"热量低于目标 {-kcal_gap:.0f} kcal")

    if combo.protein >= targets.protein * 0.85:
        combo.reasons.append(f"蛋白质 {combo.protein:.0f} g，接近目标 {targets.protein:.0f} g")
    else:
        combo.gaps.append(f"蛋白质缺口 {targets.protein - combo.protein:.0f} g")

    if combo.fat_ratio <= 0.35:
        combo.reasons.append(f"脂肪供能比 {combo.fat_ratio * 100:.0f}%，在合理范围")
    else:
        combo.gaps.append(f"脂肪供能比 {combo.fat_ratio * 100:.0f}%，偏高")

    if combo.sodium <= 1000:
        combo.reasons.append(f"钠 {combo.sodium:.0f} mg，可控")
    else:
        combo.gaps.append(f"钠 {combo.sodium:.0f} mg，偏高")
    return combo


def is_healthy(combo: Combo, targets: Targets) -> bool:
    return (
        abs(combo.kcal - targets.kcal) <= targets.kcal * 0.15
        and combo.protein >= targets.protein * 0.8
        and combo.sodium <= 1200
    )


def score_combo(combo: Combo, targets: Targets) -> float:
    kcal_err = abs(combo.kcal - targets.kcal) / targets.kcal if targets.kcal else 1
    protein_ratio = min(combo.protein / targets.protein, 1.25) if targets.protein else 0
    # 蛋白不足要真扣分：否则「热量刚好但只有 30g 蛋白」会压过「蛋白更足」的组合
    protein_deficit = max(0.0, 0.85 - protein_ratio) * 120
    sodium_penalty = max(0.0, (combo.sodium - 1000) / 1000)
    fat_penalty = max(0.0, combo.fat_ratio - 0.33) * 1.5
    return 100 - kcal_err * 110 + protein_ratio * 55 - protein_deficit - sodium_penalty * 25 - fat_penalty * 40


def recommend(
    catalog: Sequence[Food],
    targets: Targets,
    limit: int = 3,
    max_combos: int = 40000,
) -> List[Combo]:
    """在真实营养库里枚举「主堡 + 小食 + 饮料」的组合，按目标打分。"""
    buckets: Dict[str, List[Food]] = {key: [] for key in ("burger", "side", "drink", "dessert", "other")}
    for food in catalog:
        if food.energy <= 0 and "无糖" not in food.name and "0糖" not in food.name:
            continue
        buckets[classify(food)].append(food)

    burgers = buckets["burger"] or [f for f in catalog if f.energy > 200][:10]
    sides = buckets["side"][:12]
    drinks = buckets["drink"][:12]

    if not burgers:
        return []

    combos: List[Combo] = []
    count = 0
    for burger, side, drink in product(burgers, sides or [None], drinks or [None]):
        items = [food for food in (burger, side, drink) if food is not None]
        totals = _totals(items)
        if totals["kcal"] <= 0:
            continue
        combo = Combo(items=items, **totals)
        combo.score = score_combo(combo, targets)
        combos.append(combo)
        count += 1
        if count >= max_combos:
            break

    combos.sort(key=lambda c: -c.score)
    chosen = [evaluate(combo, targets) for combo in combos[: limit * 3]]
    # 优先展示热量更贴合的，避免全是一个口味
    chosen.sort(key=lambda c: (len(c.gaps), -c.score))
    return chosen[:limit]


def profile_summary(profile: Profile) -> Dict[str, Any]:
    return {
        "bmi": round(profile.bmi, 1),
        "bmi_band": bmi_band(profile.bmi),
        "bmr": round(bmr(profile)),
        "tdee": round(tdee(profile)),
        "goal": profile.goal,
        "activity": profile.activity,
    }


def meal_plan(profile: Profile) -> Dict[str, Any]:
    """单一真源：BMI/BMR/TDEE + 三餐（含加餐）的国标目标拆分。

    前端档案屏与 Web 服务都调用它，保证两处数字一致。
    """
    summary = profile_summary(profile)
    daily_kcal = tdee(profile) * (1 + GOAL_ADJUST.get(profile.goal, 0.0))
    protein_day = profile.weight_kg * PROTEIN_PER_KG.get(profile.goal, 1.5)
    meals = {}
    for meal, share in MEAL_SHARE.items():
        t = compute_targets(profile, meal)
        meals[meal] = {
            "label": t.label,
            "share": share,
            "kcal": t.kcal,
            "protein": t.protein,
            "fat": t.fat,
            "carb": t.carb,
            "sodium": t.sodium,
        }
    return {
        "summary": summary,
        "daily_kcal": round(daily_kcal),
        "daily_protein": round(protein_day, 1),
        "meals": meals,
        "activity_label": ACTIVITY_LABEL.get(profile.activity, profile.activity),
        "goal_label": GOAL_LABEL.get(profile.goal, profile.goal),
        "guideline": GUIDELINE_NOTE,
    }
