"""位置模块：门店归一化、服务能力筛选、通勤时间估算。

麦当劳 MCP 的门店类工具（query-nearby-stores / delivery-query-stores）
返回结构随场景不同略有差异，这里统一成 Store 结构再做决策。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# 常见城市名（用于把「哈尔滨中央大街」拆成 city=哈尔滨 + keyword=中央大街）
# 真实 MCP 的 query-nearby-stores 按位置搜索时 city 必填，这里必须能拆出来。
CITY_NAMES = (
    "北京", "上海", "天津", "重庆",
    "哈尔滨", "长春", "沈阳", "大连", "济南", "青岛", "烟台", "石家庄", "唐山", "保定", "廊坊",
    "太原", "大同", "呼和浩特", "包头", "鄂尔多斯",
    "郑州", "洛阳", "开封", "南阳", "新乡",
    "南京", "苏州", "无锡", "常州", "徐州", "南通", "扬州", "盐城",
    "杭州", "宁波", "温州", "嘉兴", "金华", "绍兴", "台州",
    "合肥", "芜湖", "福州", "厦门", "泉州", "南昌", "赣州", "九江",
    "武汉", "宜昌", "襄阳", "长沙", "株洲", "衡阳", "岳阳",
    "广州", "深圳", "珠海", "佛山", "东莞", "中山", "惠州", "汕头", "湛江",
    "南宁", "桂林", "海口", "三亚",
    "成都", "绵阳", "贵阳", "遵义", "昆明", "大理", "拉萨",
    "西安", "咸阳", "兰州", "西宁", "银川", "乌鲁木齐", "克拉玛依",
)
_CITY_RE = re.compile(r"^([\u4e00-\u9fa5]{2,4}?)[市省]")


def split_city_keyword(address: str) -> Tuple[str, str]:
    """把用户输入的地址拆成 (city, keyword)，供 query-nearby-stores 使用。

    拆不出来时 city 为空，由调用方退回收藏餐厅搜索。
    """
    text = (address or "").strip()
    if not text:
        return "", ""
    match = _CITY_RE.match(text)  # 「上海市静安寺」/「黑龙江省哈尔滨」
    if match:
        city = match.group(1)
        keyword = text[match.end():].strip("市区县镇 ")
        return city, keyword or "麦当劳"
    for city in sorted(CITY_NAMES, key=len, reverse=True):  # 长名优先，避免「西安」吃掉「西宁」
        if text.startswith(city):
            keyword = text[len(city):].strip("市区县镇 ")
            return city, keyword or "麦当劳"
    return "", text


# 服务标签 -> 归一化名称
SERVICE_LABELS = {
    "dinein": "堂食",
    "takeaway": "外带",
    "drivethru": "得来速",
    "delivery": "麦乐送",
    "24h": "24小时",
}

_KEY_HINTS = {
    "code": ["storeCode", "beCode", "storeId", "restCode", "code", "id"],
    "name": ["storeName", "name", "restName", "title"],
    "address": ["address", "addr", "storeAddress", "fullAddress"],
    "distance": ["distance", "distanceMeter", "distanceMeters", "dist", "distanceValue"],
    "hours": ["businessTime", "openTime", "businessHours", "openHours", "operatingTime"],
    "open_time": ["businessStartTime", "startTime", "openAt", "openBegin"],
    "close_time": ["businessEndTime", "endTime", "closeAt", "openEnd"],
    "status": ["status", "storeStatus", "openStatus", "businessStatus"],
    "services": ["services", "serviceTypes", "supportServices", "serviceTags", "tags"],
    "city": ["cityName", "city", "cityCode"],
    "lat": ["latitude", "lat", "storeLat", "y"],
    "lng": ["longitude", "lng", "lon", "storeLng", "x"],
}


def _first(raw: Dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, "", []):
            return raw[key]
    # 大小写/下划线不敏感兜底
    flat = {str(k).replace("_", "").lower(): v for k, v in raw.items()}
    for key in keys:
        candidate = flat.get(key.replace("_", "").lower())
        if candidate not in (None, "", []):
            return candidate
    return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "y", "yes", "支持", "可", "open", "营业中"}
    return False


def _to_meters(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        # 同一字段可能是米也可能是公里
        return number * 1000 if number < 50 else number
    text = str(value).strip().lower()
    digits = "".join(ch for ch in text if ch.isdigit() or ch == ".")
    if not digits:
        return None
    number = float(digits)
    if "km" in text or "公里" in text or "千米" in text:
        return number * 1000
    if number < 50:
        return number * 1000
    return number


@dataclass
class Store:
    code: str
    name: str
    address: str = ""
    distance_m: Optional[float] = None
    hours: str = ""
    status: str = ""
    services: Set[str] = field(default_factory=set)
    city: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_coords(self) -> bool:
        """真实接口经常把经纬度填成 0.0，必须当成「没有坐标」，
        否则会拿 (0,0) 去打地图，白白消耗配额还算出错误路线。"""
        if self.lat is None or self.lng is None:
            return False
        try:
            lat, lng = float(self.lat), float(self.lng)
        except (TypeError, ValueError):
            return False
        return 3 < lat < 54 and 73 < lng < 136

    @property
    def is_open(self) -> bool:
        """真实接口用布尔值 businessStatus=True/False 表达营业状态，
        字符串化后是 "True"/"False"——只认中文关键词会把营业中的门店全判成打烊。"""
        if self.status is None or self.status == "":
            return True
        if isinstance(self.status, bool):
            return self.status
        text = str(self.status).strip().lower()
        if text in ("true", "1", "yes", "y", "open", "营业中", "正常营业"):
            return True
        if text in ("false", "0", "no", "n", "closed", "休息中", "已打烊"):
            return False
        return any(token in text for token in ("营业", "open", "正常"))

    @property
    def distance_text(self) -> str:
        if self.distance_m is None:
            return "距离未知"
        if self.distance_m >= 1000:
            return f"{self.distance_m / 1000:.1f} km"
        return f"{int(self.distance_m)} m"

    def supports(self, need: Iterable[str]) -> bool:
        need = set(need)
        if not need:
            return True
        if not self.services:
            # 服务信息缺失时不做硬性拦截，交给上层提示
            return True
        return need.issubset(self.services)


def normalize_services(value: Any) -> Set[str]:
    """把各种服务字段归一成 {dinein, takeaway, drivethru, delivery, 24h}。"""
    found: Set[str] = set()
    if value is None:
        return found

    if isinstance(value, dict):
        for key, flag in value.items():
            if _as_bool(flag):
                found.update(normalize_services(key))
        return found

    tokens: List[str] = []
    if isinstance(value, str):
        tokens = [part for part in value.replace("|", ",").replace("；", ",").replace(";", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, str):
                tokens.append(item)
            elif isinstance(item, dict):
                for key, flag in item.items():
                    if _as_bool(flag):
                        tokens.append(key)

    for token in tokens:
        text = str(token).strip().lower()
        if not text:
            continue
        if any(k in text for k in ("dine", "堂食", "eat")):
            found.add("dinein")
        if any(k in text for k in ("take", "外带", "自取", "pickup")):
            found.add("takeaway")
        if any(k in text for k in ("drive", "得来速", "车道")):
            found.add("drivethru")
        if any(k in text for k in ("deliver", "麦乐送", "外送", "配送")):
            found.add("delivery")
        if "24" in text:
            found.add("24h")
    return found


def _to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_store(raw: Dict[str, Any]) -> Optional[Store]:
    if not isinstance(raw, dict):
        return None
    code = _first(raw, _KEY_HINTS["code"]) or _first(raw, ["storeCode", "beCode"])
    name = _first(raw, _KEY_HINTS["name"])
    if not name:
        return None
    # 真实接口把营业时间拆成起止两段，界面要显示就在这里拼回来
    hours = str(_first(raw, _KEY_HINTS["hours"]) or "")
    if not hours:
        open_at = str(_first(raw, _KEY_HINTS["open_time"]) or "")
        close_at = str(_first(raw, _KEY_HINTS["close_time"]) or "")
        if open_at and close_at:
            hours = f"{open_at}-{close_at}"
    return Store(
        code=str(code or name),
        name=str(name),
        address=str(_first(raw, _KEY_HINTS["address"]) or ""),
        distance_m=_to_meters(_first(raw, _KEY_HINTS["distance"])),
        hours=hours,
        status=str(_first(raw, _KEY_HINTS["status"]) or ""),
        services=normalize_services(_first(raw, _KEY_HINTS["services"])),
        city=str(_first(raw, _KEY_HINTS["city"]) or ""),
        lat=_to_float(_first(raw, _KEY_HINTS["lat"])),
        lng=_to_float(_first(raw, _KEY_HINTS["lng"])),
        raw=raw,
    )


def extract_stores(payload: Any) -> List[Store]:
    """从任意工具返回里挖出门店列表。"""
    candidates: List[Dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if any(key in node for key in ("storeName", "beCode", "storeCode")):
                candidates.append(node)
                return
            for value in node.values():
                walk(value)

    walk(payload)
    stores: List[Store] = []
    seen = set()
    for raw in candidates:
        store = normalize_store(raw)
        if store and store.code not in seen:
            seen.add(store.code)
            stores.append(store)
    return stores


def rank_stores(
    stores: Sequence[Store],
    need: Optional[Set[str]] = None,
    max_distance_m: Optional[float] = None,
    only_open: bool = True,
) -> List[Store]:
    """按「是否满足服务 > 是否营业 > 距离」排序。"""
    need = need or set()
    result = []
    for store in stores:
        if only_open and not store.is_open:
            continue
        if max_distance_m and store.distance_m and store.distance_m > max_distance_m:
            continue
        result.append(store)

    def sort_key(store: Store):
        return (
            0 if store.supports(need) else 1,
            0 if store.is_open else 1,
            store.distance_m if store.distance_m is not None else 9e9,
        )

    result.sort(key=sort_key)
    return result


def travel_minutes(distance_m: Optional[float], mode: str = "walk") -> Optional[float]:
    """粗估通勤时间：步行 75m/min、骑行 250m/min、驾车 400m/min（含找车位）。"""
    if not distance_m:
        return None
    speed = {"walk": 75.0, "bike": 250.0, "drive": 400.0}.get(mode, 75.0)
    return round(distance_m / speed, 1)


# MET 值参考《体力活动纲要》：步行 3.5、骑行 6.8
MET_TABLE = {"walk": 3.5, "bike": 6.8}
MODE_LABEL = {"walk": "步行", "bike": "骑行", "drive": "驾车"}


@dataclass
class Commute:
    distance_m: float
    minutes: float
    mode: str
    kcal: float = 0.0
    store_name: str = ""

    @property
    def mode_label(self) -> str:
        return MODE_LABEL.get(self.mode, self.mode)

    @property
    def distance_text(self) -> str:
        return f"{self.distance_m / 1000:.1f} km" if self.distance_m >= 1000 else f"{int(self.distance_m)} m"


def commute_calories(distance_m: Optional[float], mode: str, weight_kg: float = 65.0) -> float:
    """kcal = MET × 3.5 × 体重(kg) ÷ 200 × 分钟。"""
    minutes = travel_minutes(distance_m, mode)
    if not minutes:
        return 0.0
    met = MET_TABLE.get(mode, 3.5)
    return round(met * 3.5 * weight_kg / 200 * minutes, 1)


def plan_commute(
    store: "Store",
    mode: str = "walk",
    weight_kg: float = 65.0,
) -> Optional[Commute]:
    if store.distance_m is None:
        return None
    minutes = travel_minutes(store.distance_m, mode) or 0.0
    return Commute(
        distance_m=store.distance_m,
        minutes=minutes,
        mode=mode,
        kcal=commute_calories(store.distance_m, mode, weight_kg),
        store_name=store.name,
    )


def explain_choice(store: Store, need: Set[str], mode: str = "walk") -> List[str]:
    reasons = [f"距离 {store.distance_text}"]
    if store.hours:
        reasons.append(f"营业时间 {store.hours}")
    if need:
        missing = need - store.services if store.services else set()
        if not store.services:
            reasons.append("该店未返回服务标签，建议出发前电话确认")
        elif missing:
            reasons.append(f"可能不支持：{'、'.join(SERVICE_LABELS.get(s, s) for s in sorted(missing))}")
        else:
            reasons.append(f"支持：{'、'.join(SERVICE_LABELS.get(s, s) for s in sorted(need))}")
    minutes = travel_minutes(store.distance_m, mode)
    if minutes is not None:
        label = {"walk": "步行", "bike": "骑行", "drive": "驾车"}.get(mode, "步行")
        reasons.append(f"{label}约 {minutes:g} 分钟")
    return reasons
