"""业务编排层：MCP 工具调用 → 引擎输入 → 报告输出。

这里是「真实调用麦当劳 MCP」的唯一入口，所有工具名集中在此，
便于 MCP_INTEGRATION.md 与代码一一对应核对。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import coach
from . import nutrition as nut
from . import places as plc
from . import wait as wat
from . import geo as geo_mod
from .geo import Route, TencentGeo, haversine_m, route_between
from .mcp_client import MCPClient
from .schema import SchemaError, ToolBox
from .store import Profile, RecordStore
from .wait import DeparturePlan, ETA, Order

# 工具名常量：与麦当劳 MCP Server 官方工具列表一致
T_NEARBY = "query-nearby-stores"
T_DELIVERY_STORES = "delivery-query-stores"
T_ADDRESSES = "delivery-query-addresses"
T_NUTRITION = "list-nutrition-foods"
T_MEALS = "query-meals"
T_MEAL_DETAIL = "query-meal-detail"
T_STORE_COUPONS = "query-store-coupons"
T_ORDER = "query-order"
T_ORDER_LIST = "order-list"
T_NOW = "now-time-info"

# 本项目实际依赖的工具 -> 承担的角色（doctor 自检与 tools 标注共用）
USED_TOOLS: Dict[str, str] = {
    T_NEARBY: "位置 · 附近门店",
    T_DELIVERY_STORES: "位置 · 可配送门店",
    T_ADDRESSES: "位置 · 配送地址列表",
    T_NUTRITION: "营养 · 餐品营养库",
    T_MEALS: "营养 · 门店在售菜单",
    T_MEAL_DETAIL: "营养 · 套餐组成",
    T_ORDER: "候餐 · 订单详情",
    T_ORDER_LIST: "候餐 · 历史订单",
    T_NOW: "候餐 · 服务端时间",
}


@dataclass
class MealResult:
    report: nut.NutritionReport
    suggestions: List[nut.Suggestion] = field(default_factory=list)
    fill: List[nut.Food] = field(default_factory=list)
    catalog_size: int = 0
    source: str = "MCP list-nutrition-foods"

    @property
    def empty(self) -> bool:
        return not self.report.items


class CompassService:
    def __init__(
        self,
        box: ToolBox,
        records: Optional[RecordStore] = None,
        geo: Optional[TencentGeo] = None,
    ):
        self.box = box
        self.db = records or RecordStore()
        self.geo = geo or TencentGeo()
        self._catalog: Optional[List[nut.Food]] = None

    # ------------------------------------------------------------------ 档案与目标
    def profile(self) -> Optional[Profile]:
        return self.db.get_profile()

    def profile_or_default(self) -> Profile:
        return self.db.get_profile() or Profile(height_cm=172, weight_kg=65, age=28)

    def save_profile(
        self,
        height_cm: float,
        weight_kg: float,
        age: int,
        sex: str = "male",
        activity: str = "light",
        goal: str = "lose",
    ) -> Profile:
        profile = Profile(height_cm=height_cm, weight_kg=weight_kg, age=age, sex=sex, activity=activity, goal=goal)
        self.db.save_profile(profile)
        return profile

    def targets(self, profile: Optional[Profile] = None, meal: str = "lunch") -> coach.Targets:
        return coach.compute_targets(profile or self.profile_or_default(), meal=meal)

    def recommend(self, profile: Optional[Profile] = None, meal: str = "lunch", limit: int = 3):
        profile = profile or self.profile_or_default()
        targets = self.targets(profile, meal)
        catalog = self.catalog()
        combos = coach.recommend(catalog, targets, limit=limit)
        return targets, combos, len(catalog)

    # ------------------------------------------------------------------ 通勤
    def commute(
        self,
        store: plc.Store,
        mode: str = "walk",
        origin: Optional[Tuple[float, float]] = None,
    ) -> Route:
        """真实路径优先（腾讯位置服务），否则按门店距离估算。

        真实门店接口常常不带经纬度（实测为 0.0），此时用地址反查一次坐标；
        反查同样受地图配额限制且有 7 天缓存，超限就退回估算路线。
        """
        dest = (store.lat, store.lng) if store.has_coords else None
        suspect = False
        if dest is None and self.geo and self.geo.available and store.address:
            # 带上门店距离做交叉校验：解析到同名异地时会自动换写法重试
            coords = self.geo.geocode(
                store.address, store.city or "", near=origin, expected_m=store.distance_m
            )
            if coords:
                # 反查可能命中同名异地（「南京西路」→ 江苏南京）。
                # 先校验再打地图，避免为错误坐标白耗一次路线配额。
                if origin is None or geo_mod._is_plausible(haversine_m(origin, coords), store.distance_m):
                    dest = coords
                else:
                    suspect = True
        route = route_between(origin, dest, store.distance_m, mode=mode, geo=self.geo)
        if suspect and route.source == "estimate":
            route.note = "门店地址反查异常（疑同名异地），按距离估算"
        return route

    # ------------------------------------------------------------------ 工具探测
    def available_tools(self) -> List[str]:
        return sorted(self.box.tools)

    def _call(self, *names: str, **arguments: Any) -> Any:
        """按候选名依次尝试，返回第一个成功的结果。"""
        errors = []
        for name in names:
            if not self.box.has(name):
                continue
            try:
                return self.box.call(name, **arguments)
            except SchemaError as exc:
                errors.append(str(exc))
            except Exception as exc:  # noqa: BLE001 网络/参数错误交由上层提示
                errors.append(f"{name}: {exc}")
        if errors:
            raise RuntimeError("；".join(errors))
        raise SchemaError(f"当前 MCP Server 缺少工具：{names}")

    # ------------------------------------------------------------------ 时间
    def now_with_source(self) -> Tuple[datetime, str]:
        """当前时间 + 来源（mcp=麦当劳服务端时间 / local=本机时间）。

        拿「这一餐吃什么」的默认值要用麦当劳的服务端时间，
        本机时钟不准或时区不对时，餐次推荐就会跑偏。
        """
        # 实测 now-time-info 的真实返回：时间既在顶层 datetime，也在 data 子对象里
        # （data 中是 timestamp 毫秒 + datetime + formatted）。两层都要找，
        # 否则会静默退回本机时间，界面上还看不出来。
        keys = ("timestamp", "datetime", "time", "formatted", "currentTime", "serverTime")
        try:
            payload = self._call(T_NOW)
            layers = [payload]
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                layers.append(payload["data"])
            for layer in layers:
                for key in keys:
                    parsed = wat.parse_time(layer.get(key))
                    if parsed:
                        return parsed, "mcp"
        except Exception:  # noqa: BLE001 工具不可用时用本地时间，不影响主流程
            pass
        return datetime.now(), "local"

    def now(self) -> datetime:
        return self.now_with_source()[0]

    def current_meal(self) -> Dict[str, Any]:
        """按当前时间推荐餐次：进入「这一餐吃什么」时的默认值。"""
        from .coach import MEAL_LABEL, meal_of_time, meal_window_text

        moment, source = self.now_with_source()
        meal = meal_of_time(moment)
        return {
            "meal": meal,
            "meal_label": MEAL_LABEL.get(meal, meal),
            "time": moment.strftime("%Y-%m-%d %H:%M"),
            "hour": moment.hour,
            "source": source,  # mcp | local
            "window": meal_window_text(meal),
        }

    # ------------------------------------------------------------------ 门店
    def find_stores(
        self,
        address: str,
        need: Optional[set] = None,
        max_distance_m: Optional[float] = None,
        limit: int = 5,
        prefer_delivery: bool = False,
    ) -> List[plc.Store]:
        """查询附近门店（MCP `query-nearby-stores`）。

        真实接口的必填入参与 mock 不同（实测 2026-10）：
          - `beType` 必传：1=到店自提 / 5=得来速
          - `searchType` 必传：2=按位置搜索（此时 `city` 必填）/ 1=收藏餐厅
          - 只传 keyword/address 会直接返回 400 缺少参数
        因此这里先拆出「城市 + 关键词」，按位置搜索；搜不到再退回收藏/店名搜索。
        """
        need = need or set()
        city, keyword = plc.split_city_keyword(address)
        be_type = 5 if "drivethru" in need else 1

        attempts: List[Dict[str, Any]] = []
        if prefer_delivery:
            attempts.append({"tool": T_DELIVERY_STORES, "args": {}})  # 需要 addressId，缺则跳过
        if city:
            # 主路径：按位置搜索（关键词优先用地址里的地标）
            attempts.append({"tool": T_NEARBY, "args": {"beType": be_type, "searchType": 2, "city": city, "keyword": keyword}})
            if keyword and keyword != "麦当劳":
                # 地标搜不到时，退回按店名搜该城市（实测「城市+麦当劳」最稳）
                attempts.append({"tool": T_NEARBY, "args": {"beType": be_type, "searchType": 2, "city": city, "keyword": "麦当劳"}})
        attempts.append({"tool": T_NEARBY, "args": {"beType": be_type, "searchType": 1, "keyword": address}})

        stores: List[plc.Store] = []
        for attempt in attempts:
            tool_name = attempt["tool"]
            if not self.box.has(tool_name):
                continue
            if tool_name == T_DELIVERY_STORES:
                continue  # 需要 addressId，本流程不提供
            try:
                payload = self.box.call(tool_name, **attempt["args"])
            except Exception:  # noqa: BLE001 一个接口失败时尝试下一个参数组合
                continue
            stores = plc.extract_stores(payload)
            if stores:
                # 门店接口不返回城市，但后续要用它做地理编码的 region：
                # 否则「南京西路1601号」会被腾讯解析到江苏南京去。
                for store in stores:
                    if not store.city:
                        store.city = city
                break
        if not stores and city and be_type == 5:
            # 得来速没有结果时，退回到店自提再试一次
            try:
                payload = self.box.call(T_NEARBY, **{"beType": 1, "searchType": 2, "city": city, "keyword": keyword or "麦当劳"})
                stores = plc.extract_stores(payload)
            except Exception:  # noqa: BLE001
                stores = []
        return plc.rank_stores(stores, need=need, max_distance_m=max_distance_m)[:limit]

    def delivery_addresses(self) -> List[str]:
        try:
            payload = self._call(T_ADDRESSES)
        except Exception:  # noqa: BLE001
            return []
        texts: List[str] = []
        if isinstance(payload, dict):
            for value in payload.values():
                if isinstance(value, list):
                    for entry in value:
                        if isinstance(entry, dict):
                            for key in ("address", "addr", "addressDetail", "detail"):
                                if entry.get(key):
                                    texts.append(str(entry[key]))
                                    break
        return texts

    def menu(self, store_code: str = "", keyword: str = "") -> List[Dict[str, Any]]:
        arguments: Dict[str, Any] = {}
        if store_code:
            arguments["storeCode"] = store_code
        if keyword:
            arguments["keyword"] = keyword
        try:
            payload = self._call(T_MEALS, **arguments)
        except Exception:  # noqa: BLE001
            return []
        items: List[Dict[str, Any]] = []

        def walk(node: Any) -> None:
            if isinstance(node, list):
                for entry in node:
                    walk(entry)
            elif isinstance(node, dict):
                if node.get("name") or node.get("mealName") or node.get("productName"):
                    items.append(node)
                for value in node.values():
                    if isinstance(value, (list, dict)):
                        walk(value)

        walk(payload)
        return items[:60]

    # ------------------------------------------------------------------ 营养
    def catalog(self, refresh: bool = False) -> List[nut.Food]:
        if self._catalog is not None and not refresh:
            return self._catalog
        try:
            payload = self._call(T_NUTRITION)
            self._catalog = nut.extract_foods(payload)
        except Exception:  # noqa: BLE001
            self._catalog = []
        return self._catalog

    def analyse_meal(self, spec: str, budget: Optional[float] = None) -> MealResult:
        catalog = self.catalog()
        parsed = nut.parse_items(spec)
        line_items: List[nut.LineItem] = []
        unmatched: List[str] = []

        for name, qty in parsed:
            food = nut.match_food(name, catalog) if catalog else None
            if food is None:
                unmatched.append(name)
                # 无数据时保留条目，但营养记为 0，并在报告中提示
                food = nut.Food(name=name)
            line_items.append(nut.LineItem(food=food, qty=qty))

        report = nut.analyze(line_items, budget=budget)
        report.unmatched = unmatched
        if unmatched:
            note = f"以下餐品未在 MCP 营养库中匹配到，未计入营养合计：{'、'.join(unmatched)}"
            report.flags.insert(0, nut.Flag("warn", note))

        suggestions = nut.swap_suggestions(line_items, catalog, report) if catalog else []
        fill = nut.fill_to_budget(report, catalog) if catalog else []
        return MealResult(
            report=report,
            suggestions=suggestions,
            fill=fill,
            catalog_size=len(catalog),
        )

    # ------------------------------------------------------------------ 候餐
    def latest_order(self) -> Optional[Order]:
        try:
            payload = self._call(T_ORDER_LIST)
        except Exception:  # noqa: BLE001
            return None
        orders = wat.extract_orders(payload)
        if not orders:
            return None
        orders.sort(key=lambda o: o.created_at or datetime.min, reverse=True)
        return orders[0]

    def order_history(self, limit: int = 5, with_items: bool = True) -> List[Order]:
        """最近 N 单（`order-list`）→ 给「记录」屏回填真实吃过的东西。

        `order-list` 只给订单骨架（**没有商品明细**），明细要逐单用 `query-order`
        补齐；营养再拿营养库 `list-nutrition-foods` 按名字匹配，见 order_nutrition()。
        """
        try:
            payload = self._call(T_ORDER_LIST)
        except Exception:  # noqa: BLE001
            return []
        orders = wat.extract_orders(payload)
        if not orders:
            return []
        orders.sort(key=lambda o: o.created_at or datetime.min, reverse=True)
        orders = orders[: max(1, limit)]

        if not with_items:
            return orders
        for order in orders:
            if not order.order_id:
                continue
            # order-list 其实给了商品名和套餐子项，但**没有单价**；
            # 单价只有 query-order 才给。营养库查不到的商品要靠单价
            # 按「同类价格最近」估算，所以缺价时必须补一次详情。
            has_price = order.items and all(item.get("price") for item in order.items)
            if has_price:
                continue
            try:
                detail = self.order_detail(order.order_id)
            except Exception:  # noqa: BLE001 明细拉不到就保留骨架，不整单丢掉
                detail = None
            if detail and detail.items:
                order.items = detail.items
                if detail.amount and not order.amount:
                    order.amount = detail.amount
                if detail.store_name and not order.store_name:
                    order.store_name = detail.store_name
            # 详情也拿不到单价时，用订单实付金额按件数均摊，至少有个量级
            if order.items and not all(item.get("price") for item in order.items):
                total = _num_amount(order.amount)
                if total > 0:
                    each = round(total / len(order.items), 2)
                    for item in order.items:
                        if not item.get("price"):
                            item["price"] = each
        return orders

    def order_nutrition(self, order: Order) -> Dict[str, Any]:  # noqa: C901 三级回退，拆开反而更难读
        """把订单里的商品名匹配到营养库，算出这一单的营养合计。

        三级匹配，口径从硬到软，每一级都在结果里如实标注：
          ① 精确/近似匹配营养库（真实值）
          ② 套餐名拆成子项（comboItemList）逐项匹配（真实值）
          ③ 子项也查不到 → 按「同类别 + 价格最接近」的价格锚点估算（标注为估算）
        估算项会单独列在 estimates 里，绝不当成真实数据展示。
        """
        try:
            catalog = self.catalog()
        except Exception:  # noqa: BLE001 营养库不可用时只返回空，订单本身照样展示
            catalog = []
        total = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carb": 0.0, "sodium": 0.0}
        matched, misses, estimates, hits = 0, [], [], []

        def add(food, qty: float) -> None:
            total["kcal"] += food.energy * qty
            total["protein"] += food.protein * qty
            total["fat"] += food.fat * qty
            total["carb"] += food.carb * qty
            total["sodium"] += food.sodium * qty

        def add_values(values: Dict[str, float], qty: float) -> None:
            for key in ("kcal", "protein", "fat", "carb", "sodium"):
                total[key] += float(values.get(key) or 0) * qty

        for item in order.items or []:
            name = str(item.get("name") or item.get("productName") or "").strip()
            if not name:
                continue
            qty = float(item.get("qty") or item.get("quantity") or 1) or 1.0
            price = float(item.get("price") or 0) or 0.0

            # ① 直接匹配
            food = nut.match_food(name, catalog) if catalog else None
            if food:
                matched += 1
                add(food, qty)
                self._remember_anchor(name, price, food)
                # 溯源：把「这一项被认成了营养库里的哪一个」记下来，
                # 名字对不上时用户一眼能看出来（模糊匹配会误伤，不能闷声用）
                hits.append(
                    {
                        "name": name,
                        "matched_to": food.name,
                        "kcal": round(food.energy * qty, 1),
                        "via": "exact" if food.name == name else "fuzzy",
                        "score": round(nut.similarity(name, food.name), 2),
                    }
                )
                continue

            # ② 拆套餐子项
            combo_hit = 0
            unmatched_children: List[Dict[str, Any]] = []
            children = item.get("combo") or []
            per_price = price / len(children) if (price > 0 and children) else 0.0
            for child in children:
                child_name = str(child.get("name") or "").strip()
                if not child_name:
                    continue
                child_food = nut.match_food(child_name, catalog) if catalog else None
                if child_food:
                    combo_hit += 1
                    add(child_food, float(child.get("qty") or 1) * qty)
                    self._remember_anchor(
                        child_name,
                        float(child.get("price") or (price / len(children) if price > 0 else 0)),
                        child_food,
                    )
                    hits.append(
                        {
                            "name": child_name,
                            "matched_to": child_food.name,
                            "kcal": round(child_food.energy * float(child.get("qty") or 1) * qty, 1),
                            "via": "combo",
                            "score": round(nut.similarity(child_name, child_food.name), 2),
                        }
                    )
                else:
                    unmatched_children.append(child)
            # 套餐按「一项」计数（子项只是它的拆分方式），否则 matched 会比 total 还大
            if combo_hit:
                matched += 1
                # 套餐里总有一两样新品查不到（如季节限定堡），
                # 不能因为部分命中就把剩下的悄悄丢掉 —— 剩下的走价格估算补上
                for child in unmatched_children:
                    child_name = str(child.get("name") or "").strip()
                    if not child_name:
                        continue
                    child_price = float(child.get("price") or 0) or per_price
                    estimated = self._estimate_item(
                        child_name, child_price, float(child.get("qty") or 1) * qty, add_values
                    )
                    if estimated:
                        estimates.append(estimated)
                    else:
                        misses.append(child_name)
                continue

            # ③ 整个套餐都查不到：逐个子项按「同类别 + 价格最接近」估算
            if children:
                for child in children:
                    child_name = str(child.get("name") or "").strip()
                    if not child_name:
                        continue
                    child_price = float(child.get("price") or 0) or per_price
                    estimated = self._estimate_item(
                        child_name, child_price, float(child.get("qty") or 1) * qty, add_values
                    )
                    if estimated:
                        estimates.append(estimated)
                    else:
                        misses.append(child_name)
            else:
                estimated = self._estimate_item(name, price, qty, add_values)
                if estimated:
                    estimates.append(estimated)
                else:
                    misses.append(name)

        result = {key: round(value, 1) for key, value in total.items()}
        result["matched"] = matched
        result["total"] = len(order.items or [])
        result["misses"] = misses
        result["estimates"] = estimates
        result["hits"] = hits
        return result

    def _remember_anchor(self, name: str, price: float, food: Any) -> None:
        """把「这个商品卖多少钱、营养是多少」沉淀下来，供以后估算新品。

        营养库没有价格、订单没有营养，两边靠名字对上之后才有完整的锚点。
        """
        if not name or price <= 0:
            return
        try:
            self.db.upsert_food_anchor(
                name=name,
                category=nut.guess_category(name),
                price=price,
                kcal=food.energy,
                protein=food.protein,
                fat=food.fat,
                carb=food.carb,
                sodium=food.sodium,
            )
        except Exception:  # noqa: BLE001 锚点写不进去不影响算这一单
            pass

    def _estimate_item(self, name: str, price: float, qty: float, add_values) -> Optional[Dict[str, Any]]:
        """营养库查不到时，按「同类别 + 价格最接近」的锚点估算这一项。

        锚点来自以往订单里「名字能对上营养库」的商品（既有价格又有营养）。
        冷启动时锚点为空，返回 None —— 宁可标成未知，也不凭空编一个数。
        """
        if price <= 0:
            return None
        category = nut.guess_category(name)
        try:
            anchors = self.db.food_anchors(category=category)
        except Exception:  # noqa: BLE001
            return None
        if not anchors:
            return None
        best = min(anchors, key=lambda a: abs(float(a.get("price") or 0) - price))
        add_values(best, qty)
        return {
            "name": name,
            "price": round(price, 1),
            "category": category,
            "category_label": nut.CATEGORY_LABEL.get(category, category),
            "based_on": best.get("name"),
            "based_on_price": float(best.get("price") or 0),
            "kcal": round(float(best.get("kcal") or 0) * qty, 1),
        }

    def order_detail(self, order_id: Optional[str] = None) -> Optional[Order]:
        if order_id:
            try:
                payload = self._call(T_ORDER, orderId=order_id)
                order = wat.normalize_order(payload)
                if order:
                    return order
            except Exception:  # noqa: BLE001 落到历史订单兜底
                pass
            return None
        return self.latest_order()

    def wait_state(
        self,
        order: Order,
        travel_min: float = 10.0,
        buffer_min: float = 2.0,
    ) -> Tuple[ETA, DeparturePlan]:
        now = self.now()
        eta = wat.estimate_eta(order, now)
        plan = wat.plan_departure(eta, travel_min=travel_min, now=now, buffer_min=buffer_min)
        return eta, plan

    # ------------------------------------------------------------------ 落库
    def log_combo(self, combo: coach.Combo, targets: coach.Targets, store_name: str = "") -> int:
        items = [{"name": food.name, "qty": 1} for food in combo.items]
        return self.db.log_meal(
            items=items,
            kcal=combo.kcal,
            protein=combo.protein,
            fat=combo.fat,
            carb=combo.carb,
            sodium=combo.sodium,
            target_kcal=targets.kcal,
            store=store_name,
            meal_type=targets.meal,
            on_target=coach.is_healthy(combo, targets),
        )

    def log_commute(self, store: plc.Store, route: Route, weight_kg: float) -> int:
        return self.db.log_commute(
            store=store.name,
            mode=route.mode,
            distance_m=route.distance_m,
            minutes=route.minutes,
            kcal=route.calories(weight_kg),
        )

    def stats(self) -> Dict[str, Any]:
        return self.db.stats()

    def badges(self, stats: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        return self.db.achievements(stats)


def _num_amount(value: Any) -> float:
    """订单金额可能是 "31.9" / "¥5" / 31.9，统一取数字。"""
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group()) if match else 0.0


def build_service(settings, mock: bool = False, require_auth: bool = True) -> Tuple[CompassService, MCPClient]:
    """按模式构造服务：mock 读 fixtures，真实模式走 MCP。

    require_auth=False 供 doctor 使用：未配置 Token 时也要能打印自检信息，
    而不是在构造阶段就抛异常。
    """
    geo = TencentGeo(getattr(settings, "tmap_key", "") or "")
    if mock or settings.mock:
        from .mock import MockBox

        box = MockBox()
        return CompassService(box, geo=geo), MCPClient(settings)  # 客户端仅用于信息展示

    client = MCPClient(settings)
    if require_auth:
        client.ensure_ready()
    return CompassService(ToolBox(client), geo=geo), client
