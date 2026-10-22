"""营养模块：餐品营养归一化、整餐营养拆解、轻量替换建议。

营养数据全部来自麦当劳 MCP 的 list-nutrition-foods 工具，本模块不内置任何
餐品营养数值；只有「替换规则」里的差值是基于常见份量差的经验估算，会在输出
中明确标注为估算值，避免误导用户。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DAILY_REFERENCE_KCAL = 2000.0

_KEYS = {
    "name": ["name", "foodName", "productName", "mealName", "title", "cnName"],
    "code": ["code", "foodCode", "productCode", "mealCode", "sku"],
    "energy": ["energy", "calorie", "calories", "kcal", "energyKcal", "热量", "能量"],
    "protein": ["protein", "proteinG", "蛋白质"],
    "fat": ["fat", "fatG", "脂肪"],
    "carb": ["carbohydrate", "carb", "carbonhydrate", "carbs", "碳水化合物"],
    "sodium": ["sodium", "sodiumMg", "钠"],
    "calcium": ["calcium", "calciumMg", "钙"],
    "category": ["category", "categoryName", "type", "分类"],
    "unit": ["unit", "serving", "portion", "spec", "份量"],
}


def _num(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "")
    match = re.search(r"-?\d+(\.\d+)?", text)
    return float(match.group()) if match else 0.0


def _pick(raw: Dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    flat = {str(k).replace("_", "").lower(): v for k, v in raw.items()}
    for key in keys:
        found = flat.get(key.replace("_", "").lower())
        if found not in (None, ""):
            return found
    return None


@dataclass
class Food:
    name: str
    code: str = ""
    energy: float = 0.0
    protein: float = 0.0
    fat: float = 0.0
    carb: float = 0.0
    sodium: float = 0.0
    calcium: float = 0.0
    category: str = ""
    unit: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LineItem:
    food: Food
    qty: float = 1.0

    @property
    def energy(self) -> float:
        return self.food.energy * self.qty

    @property
    def protein(self) -> float:
        return self.food.protein * self.qty

    @property
    def fat(self) -> float:
        return self.food.fat * self.qty

    @property
    def carb(self) -> float:
        return self.food.carb * self.qty

    @property
    def sodium(self) -> float:
        return self.food.sodium * self.qty

    @property
    def calcium(self) -> float:
        return self.food.calcium * self.qty


@dataclass
class Flag:
    level: str  # high / warn / ok
    text: str


@dataclass
class Suggestion:
    title: str
    detail: str
    save_kcal: float = 0.0
    save_sodium: float = 0.0
    estimated: bool = True


_NUTRIENT_KEYS_NORM = {
    re.sub(r"[^a-z0-9]", "", key.lower())
    for key in _KEYS["energy"] + _KEYS["protein"] + _KEYS["fat"] + _KEYS["carb"] + _KEYS["sodium"] + _KEYS["calcium"]
}


def _has_nutrition(raw: Dict[str, Any]) -> bool:
    return any(re.sub(r"[^a-z0-9]", "", str(key).lower()) in _NUTRIENT_KEYS_NORM for key in raw)


def normalize_food(raw: Dict[str, Any]) -> Optional[Food]:
    if not isinstance(raw, dict):
        return None
    name = _pick(raw, _KEYS["name"])
    if not name:
        return None
    return Food(
        name=str(name).strip(),
        code=str(_pick(raw, _KEYS["code"]) or ""),
        energy=_num(_pick(raw, _KEYS["energy"])),
        protein=_num(_pick(raw, _KEYS["protein"])),
        fat=_num(_pick(raw, _KEYS["fat"])),
        carb=_num(_pick(raw, _KEYS["carb"])),
        sodium=_num(_pick(raw, _KEYS["sodium"])),
        calcium=_num(_pick(raw, _KEYS["calcium"])),
        category=str(_pick(raw, _KEYS["category"]) or ""),
        unit=str(_pick(raw, _KEYS["unit"]) or ""),
        raw=raw,
    )


# 实测 list-nutrition-foods 返回的是文本表格，而非 JSON 数组：
#   [160]{productName,nutritionDescription,energyKj,energyKcal,protein,...}:
#     猪柳麦满分,null,1288,308,16,16,24,781,213
_TABLE_HEADER = re.compile(r"\[\d+\]\s*\{(?P<cols>[^}]*)\}\s*:")


def parse_food_table(text: str) -> List[Food]:
    """解析 MCP 的文本表格格式（见上）。非表格文本返回空列表。"""
    match = _TABLE_HEADER.search(text or "")
    if not match:
        return []
    cols = [c.strip() for c in match.group("cols").split(",") if c.strip()]
    foods: List[Food] = []
    for line in (text[match.end():] or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(cols):
            continue
        if len(parts) > len(cols):  # 描述字段可能自带逗号，多出的一律并入末列
            parts = parts[: len(cols) - 1] + [",".join(parts[len(cols) - 1:])]
        # 不按 energy > 0 过滤：无糖可乐、饮用水本就是 0 kcal 的真实商品，
        # 恰恰是降卡推荐里最该出现的选项（下游推荐逻辑会自行筛掉不适用的）。
        food = normalize_food(dict(zip(cols, parts)))
        if food:
            foods.append(food)
    return foods


def extract_foods(payload: Any) -> List[Food]:
    found: Dict[str, Food] = {}

    def walk(node: Any) -> None:
        if isinstance(node, str):
            for food in parse_food_table(node):
                found.setdefault(food.name, food)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            food = normalize_food(node)
            if food and _has_nutrition(node):
                found.setdefault(food.name, food)
                return
            for value in node.values():
                walk(value)

    walk(payload)
    return list(found.values())


def _bigrams(text: str) -> set:
    cleaned = re.sub(r"[\s\-_·()（）]", "", text)
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)} or {cleaned}


def similarity(left: str, right: str) -> float:
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# 品类关键词：营养库里查不到的新品，要按「同类别 + 价格最接近」估算，
# 类别只能从商品名推断（麦当劳营养库与菜单接口都不给分类/价格）。
CATEGORY_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("breakfast", ("麦满分", "早餐", "粥", "油条", "薯饼", "咖啡", "豆浆")),
    ("dessert", ("冰淇淋", "圆筒", "圣代", "麦旋风", "派", "蛋糕", "甜筒", "奶昔", "华夫")),
    ("drink", ("可乐", "雪碧", "芬达", "茶", "果汁", "牛奶", "水", "气泡", "美式", "拿铁", "澳白")),
    ("chicken", ("鸡块", "麦乐鸡", "炸鸡", "鸡翅", "鸡腿", "鸡排", "辣翅")),
    ("beef", ("汉堡", "堡", "牛", "巨无霸", "麦香", "安格斯", "双层")),
    ("side", ("薯条", "玉米", "沙拉", "小食", "薯格", "洋葱", "虾")),
)
CATEGORY_LABEL = {
    "breakfast": "早餐",
    "dessert": "甜品",
    "drink": "饮料",
    "chicken": "炸鸡",
    "beef": "汉堡",
    "side": "小食",
    "other": "其他",
}


def guess_category(name: str) -> str:
    """从商品名推断品类，用于「同类价格最近」的兜底估算。"""
    text = str(name or "")
    for category, words in CATEGORY_RULES:
        for word in words:
            if word in text:
                return category
    return "other"


def match_food(query: str, catalog: Sequence[Food], threshold: float = 0.34) -> Optional[Food]:
    """按名称把用户口语（如「大杯可乐」）匹配到目录中的餐品。"""
    if not query or not catalog:
        return None
    query = query.strip()
    for food in catalog:
        if food.name == query:
            return food
    for food in catalog:
        if query in food.name or food.name in query:
            return food
    scored = sorted(((similarity(query, f.name), f) for f in catalog), key=lambda x: -x[0])
    if scored and scored[0][0] >= threshold:
        return scored[0][1]
    return None


def parse_items(spec: str) -> List[Tuple[str, float]]:
    """解析 "巨无霸:1,中薯条:1,可乐:2" → [('巨无霸', 1.0), ...]"""
    result: List[Tuple[str, float]] = []
    pattern = re.compile(r"^(?P<name>.+?)\s*[:：*×xX]\s*(?P<qty>\d+(?:\.\d+)?)$")
    for chunk in re.split(r"[,，;；\n]+", spec or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        matched = pattern.match(chunk)
        if matched and matched.group("name").strip():
            result.append((matched.group("name").strip(), float(matched.group("qty"))))
        else:
            result.append((chunk, 1.0))
    return result


@dataclass
class NutritionReport:
    items: List[LineItem]
    budget: Optional[float] = None
    total_energy: float = 0.0
    total_protein: float = 0.0
    total_fat: float = 0.0
    total_carb: float = 0.0
    total_sodium: float = 0.0
    total_calcium: float = 0.0
    flags: List[Flag] = field(default_factory=list)
    unmatched: List[str] = field(default_factory=list)

    @property
    def energy_share_of_day(self) -> float:
        return self.total_energy / DAILY_REFERENCE_KCAL * 100

    @property
    def budget_used(self) -> Optional[float]:
        if not self.budget:
            return None
        return self.total_energy / self.budget * 100

    @property
    def fat_energy_ratio(self) -> float:
        if not self.total_energy:
            return 0.0
        return self.total_fat * 9 / self.total_energy * 100

    @property
    def carb_energy_ratio(self) -> float:
        if not self.total_energy:
            return 0.0
        return self.total_carb * 4 / self.total_energy * 100


has_veg_pattern = re.compile(r"沙拉|蔬菜|生菜|玉米|番茄|黄瓜|苹果|橙|鲜蔬|果蔬")


def analyze(items: Sequence[LineItem], budget: Optional[float] = None) -> NutritionReport:
    report = NutritionReport(items=list(items), budget=budget)
    report.total_energy = sum(i.energy for i in items)
    report.total_protein = sum(i.protein for i in items)
    report.total_fat = sum(i.fat for i in items)
    report.total_carb = sum(i.carb for i in items)
    report.total_sodium = sum(i.sodium for i in items)
    report.total_calcium = sum(i.calcium for i in items)

    if not items:
        report.flags.append(Flag("warn", "没有匹配到餐品，无法计算营养。请检查餐品名称或用 --food-code 指定。"))
        return report

    if budget:
        used = report.total_energy / budget
        if used > 1.15:
            report.flags.append(
                Flag("high", f"本餐 {report.total_energy:.0f} kcal，超出预算 {budget:.0f} kcal 的 {used * 100 - 100:.0f}%。")
            )
        elif used > 1.0:
            report.flags.append(Flag("warn", f"本餐 {report.total_energy:.0f} kcal，略微超出预算（{used * 100:.0f}%）。"))
        else:
            report.flags.append(Flag("ok", f"热量在预算内，结余 {budget - report.total_energy:.0f} kcal。"))

    if report.total_sodium > 1200:
        report.flags.append(
            Flag("high", f"钠 {report.total_sodium:.0f} mg，占每日上限（2000 mg）的 {report.total_sodium / 2000 * 100:.0f}%，这一餐就用掉大半配额，下一餐建议清淡。")
        )
    elif report.total_sodium > 800:
        report.flags.append(
            Flag("warn", f"钠 {report.total_sodium:.0f} mg，占每日上限的 {report.total_sodium / 2000 * 100:.0f}%，偏高，建议配无糖饮品并少用蘸酱。")
        )

    if report.fat_energy_ratio > 40:
        report.flags.append(Flag("high", f"脂肪供能比 {report.fat_energy_ratio:.0f}%，明显高于建议上限 30%。"))
    elif report.fat_energy_ratio > 32:
        report.flags.append(Flag("warn", f"脂肪供能比 {report.fat_energy_ratio:.0f}%，略高于建议区间 20%–30%。"))

    if report.total_protein < 15:
        report.flags.append(Flag("warn", f"蛋白质仅 {report.total_protein:.0f} g，正餐建议 ≥ 20 g，容易很快又饿。"))

    if not any(has_veg_pattern.search(i.food.name) for i in items):
        report.flags.append(Flag("warn", "本餐没有蔬菜水果，膳食纤维和维生素偏少。"))

    return report


# 经验替换规则：(匹配, 标题, 说明, 估算节省热量, 估算节省钠)
SWAP_RULES: List[Tuple[str, str, str, float, float]] = [
    (r"可乐|雪碧|芬达|汽水", "饮料换无糖", "大杯含糖饮料换成无糖版本，甜味几乎不变。", 150.0, 0.0),
    (r"大薯|大份薯条", "薯条降一档", "大薯换中薯，脆感不变，热量直接降一档。", 130.0, 90.0),
    (r"中薯|中份薯条", "薯条降一档", "中薯换小薯，或与同伴分食。", 110.0, 80.0),
    (r"双层|巨无霸|安格斯|厚牛", "主堡降一档", "双层/厚牛换单层经典款，蛋白质仍在但脂肪显著下降。", 230.0, 260.0),
    (r"炸鸡|麦乐鸡|鸡块|鸡排|鸡腿堡", "主菜换烤制", "炸物换板烧/烤鸡类，减少裹粉吸油。", 180.0, 210.0),
    (r"奶昔|冰淇淋|新地|麦旋风|圣代", "甜品换小份", "奶昔换小杯，或改甜筒。", 200.0, 40.0),
    (r"派|薯饼", "油炸小食替换", "派/薯饼属油炸小食，可换成玉米杯。", 160.0, 30.0),
    (r"酱|沙拉酱|甜酸酱", "少酱或不加酱", "蘸酱与抹酱是隐形钠与脂肪来源，减半即可。", 60.0, 120.0),
]


def swap_suggestions(
    items: Sequence[LineItem],
    catalog: Sequence[Food],
    report: Optional[NutritionReport] = None,
) -> List[Suggestion]:
    """生成可执行的替换建议：先按规则命中，再找同类低卡替代品。"""
    suggestions: List[Suggestion] = []
    seen_titles = set()
    covered: set = set()

    # 第一轮：数据驱动的同类替代（营养值直接取自 MCP，最可信）
    for item in items:
        if item.food.energy <= 0:
            continue
        alternatives = [
            food
            for food in catalog
            if food is not item.food
            and food.energy > 0
            and food.energy < item.food.energy * 0.8
            and similarity(food.name, item.food.name) >= 0.3
        ]
        if not alternatives:
            continue
        best = min(alternatives, key=lambda f: f.energy)
        title = f"换成 {best.name}"
        if title in seen_titles:
            continue
        seen_titles.add(title)
        covered.add(item.food.name)
        suggestions.append(
            Suggestion(
                title=title,
                detail=(
                    f"{item.food.name}（{item.food.energy:.0f} kcal）→ {best.name}"
                    f"（{best.energy:.0f} kcal），营养数据均取自 MCP 返回结果。"
                ),
                save_kcal=(item.food.energy - best.energy) * item.qty,
                save_sodium=max(0.0, (item.food.sodium - best.sodium) * item.qty),
                estimated=False,
            )
        )

    # 第二轮：经验规则补充（去酱、换烤制、换无糖等营养库里没有对应品的场景）
    for item in items:
        if item.food.name in covered:
            continue
        no_sugar_drink = any(token in item.food.name for token in ("无糖", "0糖", "零度"))
        for pattern, title, detail, save_kcal, save_sodium in SWAP_RULES:
            if title in seen_titles or not re.search(pattern, item.food.name):
                continue
            if title == "饮料换无糖" and no_sugar_drink:
                continue
            seen_titles.add(title)
            suggestions.append(
                Suggestion(
                    title=title,
                    detail=f"{item.food.name} → {detail}",
                    save_kcal=save_kcal * item.qty,
                    save_sodium=save_sodium * item.qty,
                )
            )
            break

    suggestions.sort(key=lambda s: -s.save_kcal)
    return suggestions


def fill_to_budget(report: NutritionReport, candidates: Sequence[Food], limit: int = 3) -> List[Food]:
    """在预算剩余额度内推荐可以补的餐品（优选中低热量、含蛋白质）。"""
    if not report.budget:
        return []
    remaining = report.budget - report.total_energy
    if remaining < 60:
        return []
    pool = [
        food
        for food in candidates
        if 0 < food.energy <= remaining and food.name not in {i.food.name for i in report.items}
    ]
    pool.sort(key=lambda f: (-f.protein, f.energy))
    return pool[:limit]


def format_number(value: float, unit: str = "", digits: int = 0) -> str:
    text = f"{value:.{digits}f}"
    return f"{text}{unit}" if unit else text
