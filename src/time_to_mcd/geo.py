"""地图层：真实路径规划 + 合规地图页。

合规约束（必须遵守）：
  - 底图只使用国内合规服务商：腾讯 / 高德 / 百度 / 天地图。本项目统一用腾讯位置服务。
  - 前端一律不内置任何可用 Key：
        · 默认（无 Key）：走腾讯地图 GL JS 的密钥代理模式 `_TMapSecurityConfig`，前端零 Key；
        · 自备 Key（T2M_TMAP_KEY）：Key 只在命令行进程内用于**后端** WebService 请求，
          生成的本地地图页才会带上，且不进入仓库（.env / 环境变量）。
  - 坐标系使用 GCJ-02；不做任何边界、辖区的自定义绘制，底图由服务商提供。

两层能力：
  1. `TencentGeo`：地址解析、步行/骑行路径规划 → 真实距离与时长（用于校准通勤热量）。
  2. `render_map_html`：生成单文件地图页（门店 + 起点 + 路线 + 消耗面板）。
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

TENCENT_BASE = "https://apis.map.qq.com/ws"
MODE_PATHS = {"walk": "walking", "bike": "bicycling", "drive": "driving"}

# 腾讯位置服务调用配额（个人开发者免费额度有限，必须强制限流）
DEFAULT_MAX_PER_SECOND = 1
DEFAULT_MAX_PER_DAY = 100
ROUTE_CACHE_TTL = 7 * 24 * 3600  # 路线缓存 7 天：同一段路只消耗一次配额

# 无 Key 时的估算参数（直线距离 × 绕行系数）
DETOUR_FACTOR = 1.25
SPEED_M_PER_MIN = {"walk": 75.0, "bike": 250.0, "drive": 400.0}
MET_TABLE = {"walk": 3.5, "bike": 6.8}
MODE_LABEL = {"walk": "步行", "bike": "骑行", "drive": "驾车"}

# 腾讯常见状态码 → 人话提示。
# 实测：同一把 Key 下不同服务是**分别配额**的，逆地址解析能用、
# 地址解析却返回 121 —— 只报「按距离估算」用户根本不知道要去控制台补额度。
STATUS_HINTS = {
    121: "该服务当日额度用尽或未在控制台分配配额",
    110: "请求来源未被授权（Key 未配置域名白名单）",
    120: "Key 无效或已被删除",
    199: "请求过于频繁（超出服务商侧并发/频次）",
    302: "参数错误",
    311: "Key 缺少该接口的调用权限",
}


def _status_text(status: Any, message: str) -> str:
    """把腾讯的状态码翻译成一句用户看得懂的原因。"""
    hint = STATUS_HINTS.get(status)
    message = (message or "").strip()
    if hint and message:
        return f"腾讯位置服务返回 {status}：{message}（{hint}）"
    if hint:
        return f"腾讯位置服务返回 {status}：{hint}"
    if message:
        return f"腾讯位置服务返回 {status}：{message}"
    return f"腾讯位置服务返回 {status}"


@dataclass
class Route:
    distance_m: float
    minutes: float
    mode: str = "walk"
    source: str = "estimate"  # tencent | estimate
    steps: List[str] = field(default_factory=list)
    polyline: List[Tuple[float, float]] = field(default_factory=list)
    origin: Optional[Tuple[float, float]] = None
    dest: Optional[Tuple[float, float]] = None
    note: str = ""  # 降级原因 / 缓存说明，供界面如实标注

    @property
    def mode_label(self) -> str:
        return MODE_LABEL.get(self.mode, self.mode)

    @property
    def kcal(self) -> float:
        return 0.0  # 由调用方结合体重计算，见 calories()

    def calories(self, weight_kg: float) -> float:
        met = MET_TABLE.get(self.mode, 3.5)
        return round(met * 3.5 * weight_kg / 200 * self.minutes, 1)

    @property
    def distance_text(self) -> str:
        return f"{self.distance_m / 1000:.1f} km" if self.distance_m >= 1000 else f"{int(self.distance_m)} m"


def haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(h))


def decode_polyline(coors: Sequence[Any]) -> List[Tuple[float, float]]:
    """腾讯路径 polyline 是前向差分压缩数组，前两个值为绝对坐标（×1e6）。"""
    try:
        values = [float(v) for v in coors]
    except (TypeError, ValueError):
        return []
    if len(values) < 4:
        return []
    points = [(values[0] / 1e6, values[1] / 1e6)]
    lat, lng = values[0], values[1]
    for index in range(2, len(values) - 1, 2):
        lat += values[index]
        lng += values[index + 1]
        points.append((lat / 1e6, lng / 1e6))
    # 坐标合法性兜底
    if any(not (3 < p[0] < 54 and 73 < p[1] < 136) for p in points[:5]):
        return []
    return points


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


def _city_prefix(city: str) -> str:
    """「上海」→「上海市」，拼在门牌前做地址解析。"""
    city = (city or "").strip()
    if not city:
        return ""
    return city if city.endswith(("市", "区", "县", "省")) else city + "市"


def _address_candidates(address: str, city: str) -> List[str]:
    """生成地址解析的候选写法，越靠前越可能命中正确城市。

    实测（同一条路名在多个城市都存在）：
      「XX路169号」              → 解析到外市（30.95, 120.39）
      「示例市XX路169号」        → 目标位置（31.23, 121.46）✔
    """
    address = (address or "").strip()
    prefix = _city_prefix(city)
    out: List[str] = []
    if prefix and not address.startswith(prefix) and not address.startswith(city.strip()):
        out.append(prefix + address)
    out.append(address)
    return out


def _round_coord(point: Tuple[float, float]) -> Tuple[float, float]:
    """缓存键用的坐标精度（约 11 m）：起点稍动也能命中缓存，省配额。"""
    return round(float(point[0]), 4), round(float(point[1]), 4)


class GeoQuota:
    """腾讯位置服务调用配额：每秒上限 + 当日上限 + 结果缓存。

    - 秒级窗口：进程内滑动窗口（1 秒），默认每秒最多 1 次。
    - 当日计数：落盘到 data/geo_quota.json，**重启进程不会重置**，跨进程也累加。
    - 结果缓存：同一段路线 7 天内只消耗一次配额（前端每 5 秒轮询也不会打爆）。
    - 超限不报错，由调用方降级为按距离估算，界面如实标注。
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        max_per_second: Optional[int] = None,
        max_per_day: Optional[int] = None,
        cache_ttl: int = ROUTE_CACHE_TTL,
    ):
        self.path = Path(path) if path else Path(__file__).resolve().parents[2] / "data" / "geo_quota.json"
        self.max_per_second = max_per_second if max_per_second is not None else _env_int("T2M_TMAP_MAX_PER_SEC", DEFAULT_MAX_PER_SECOND)
        self.max_per_day = max_per_day if max_per_day is not None else _env_int("T2M_TMAP_MAX_PER_DAY", DEFAULT_MAX_PER_DAY)
        self.cache_ttl = cache_ttl
        self._lock = threading.Lock()
        self._second_hits: List[float] = []
        self._state: Dict[str, Any] = {"date": "", "used_today": 0, "cache": {}}
        self._load()

    # ---------------------------------------------------------------- 持久化
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        self._state = {
            "date": str(raw.get("date") or ""),
            "used_today": int(raw.get("used_today") or 0),
            "cache": raw.get("cache") if isinstance(raw.get("cache"), dict) else {},
        }

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # 配额文件写不进去也不该打断主流程

    def _roll_day(self) -> None:
        today = date.today().isoformat()
        if self._state.get("date") != today:
            self._state = {"date": today, "used_today": 0, "cache": {}}

    # ---------------------------------------------------------------- 限流
    def allow(self) -> Tuple[bool, str]:
        """申请一次调用额度。返回 (是否放行, 未放行原因)。"""
        with self._lock:
            now = time.time()
            self._second_hits = [t for t in self._second_hits if now - t < 1.0]
            if len(self._second_hits) >= self.max_per_second:
                wait = max(0.1, 1.0 - (now - self._second_hits[0]))
                return False, f"地图调用过快：每秒最多 {self.max_per_second} 次（约 {wait:.1f} 秒后恢复），本次按距离估算"
            self._roll_day()
            if self._state["used_today"] >= self.max_per_day:
                return False, f"地图调用已达当日上限 {self.max_per_day} 次（明日自动恢复），本次按距离估算"
            self._second_hits.append(now)
            self._state["used_today"] += 1
            self._save()
            return True, ""

    def allow_wait(self, max_wait: float = 3.0) -> Tuple[bool, str]:
        """等一小会儿再申请额度。

        一次「找附近门店」要连着算 3 家店的路线，秒级限流会让第 2、3 家直接降级。
        这里对「每秒限流」排队等待（最多 max_wait 秒），当日额度用尽则立刻放弃——
        那种情况等也没用，不如马上降级并如实标注。
        """
        deadline = time.time() + max_wait
        while True:
            allowed, reason = self.allow()
            if allowed:
                return True, ""
            if "当日上限" in reason:
                return False, reason
            now = time.time()
            if now >= deadline:
                return False, reason
            time.sleep(min(0.4, deadline - now))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            self._second_hits = [t for t in self._second_hits if now - t < 1.0]
            self._roll_day()
            cache = self._state.get("cache") or {}
            return {
                "available": True,
                "used_second": len(self._second_hits),
                "max_per_second": self.max_per_second,
                "used_today": int(self._state["used_today"]),
                "max_per_day": self.max_per_day,
                "left_today": max(0, self.max_per_day - int(self._state["used_today"])),
                "cached_routes": len(cache),
                "date": self._state.get("date") or date.today().isoformat(),
            }

    # ---------------------------------------------------------------- 缓存
    def cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = (self._state.get("cache") or {}).get(key)
        if not isinstance(item, dict):
            return None
        if time.time() - float(item.get("at") or 0) > self.cache_ttl:
            return None
        return item.get("value") if isinstance(item.get("value"), dict) else None

    def cache_put(self, key: str, value: Dict[str, Any]) -> None:
        with self._lock:
            self._roll_day()
            cache = self._state.setdefault("cache", {})
            cache[key] = {"at": time.time(), "value": value}
            # 控制文件体积：只保留最近 500 条
            if len(cache) > 500:
                for old in sorted(cache, key=lambda k: cache[k].get("at", 0))[: len(cache) - 500]:
                    cache.pop(old, None)
            self._save()


_DEFAULT_QUOTA_LOCK = threading.Lock()
_DEFAULT_QUOTA: Optional[GeoQuota] = None


def default_quota() -> GeoQuota:
    """进程内共享一个配额实例，保证多入口（CLI / Web）统一计数。"""
    global _DEFAULT_QUOTA
    with _DEFAULT_QUOTA_LOCK:
        if _DEFAULT_QUOTA is None:
            _DEFAULT_QUOTA = GeoQuota()
        return _DEFAULT_QUOTA


def _route_to_json(route: Route) -> Dict[str, Any]:
    return {
        "distance_m": route.distance_m,
        "minutes": route.minutes,
        "mode": route.mode,
        "source": route.source,
        "steps": route.steps,
        "polyline": [list(p) for p in route.polyline],
        "origin": list(route.origin) if route.origin else None,
        "dest": list(route.dest) if route.dest else None,
    }


def _route_from_json(data: Dict[str, Any]) -> Optional[Route]:
    try:
        origin = data.get("origin")
        dest = data.get("dest")
        return Route(
            distance_m=float(data.get("distance_m") or 0),
            minutes=float(data.get("minutes") or 0),
            mode=str(data.get("mode") or "walk"),
            source=str(data.get("source") or "tencent"),
            steps=[str(s) for s in (data.get("steps") or [])],
            polyline=[(float(p[0]), float(p[1])) for p in (data.get("polyline") or []) if len(p) >= 2],
            origin=tuple(origin) if origin else None,
            dest=tuple(dest) if dest else None,
        )
    except (TypeError, ValueError, IndexError):
        return None


class TencentGeo:
    """腾讯位置服务 WebService（仅后端调用，Key 不进入前端代码）。

    所有对外请求都先过 `GeoQuota`：超限直接返回 None，由上层降级为估算路线，
    绝不会因为限流抛异常打断主流程。
    """

    def __init__(
        self,
        key: str = "",
        timeout: float = 15.0,
        quota: Optional[GeoQuota] = None,
        wait_for_quota: bool = True,
        max_wait: float = 3.0,
    ):
        self.key = (key or "").strip()
        self.timeout = timeout
        self.quota = quota or default_quota()
        self.wait_for_quota = wait_for_quota  # 秒级限流时排队等待，而不是直接降级
        self.max_wait = max_wait
        self.blocked_reason = ""  # 最近一次被限流/不可用的原因

    @property
    def available(self) -> bool:
        return bool(self.key)

    def quota_snapshot(self) -> Dict[str, Any]:
        snap = self.quota.snapshot()
        snap["available"] = self.available
        snap["blocked_reason"] = self.blocked_reason
        return snap

    def _get(self, path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.available:
            self.blocked_reason = ""
            return None
        if self.wait_for_quota:
            allowed, reason = self.quota.allow_wait(self.max_wait)
        else:
            allowed, reason = self.quota.allow()
        if not allowed:
            self.blocked_reason = reason
            return None
        params = {**params, "key": self.key}
        url = f"{TENCENT_BASE}/{path}?{urllib.parse.urlencode(params)}"
        return self._http_json(url)

    def _http_json(self, url: str) -> Optional[Dict[str, Any]]:
        """唯一的出网口（测试可替换，确保限流逻辑本身也被覆盖）。"""
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return None
        status = payload.get("status")
        if status != 0:
            # 把腾讯的具体原因带出去（如「此key每日调用量已达到上限」），
            # 否则用户只会看到「按距离估算」，不知道是配额用完了。
            message = str(payload.get("message") or "").strip()
            self.blocked_reason = _status_text(status, message)
            return None
        return payload.get("result") or {}

    def geocode(
        self,
        address: str,
        city: str = "",
        near: Optional[Tuple[float, float]] = None,
        expected_m: Optional[float] = None,
    ) -> Optional[Tuple[float, float]]:
        """地址 → 坐标。

        踩过的坑：门店地址常常只有「吴江路169号」这种不带城市的门牌，
        直接解析会命中**同名异地**（实测解析到苏州/南京），算出几百公里的路线。
        所以这里按「城市名 + 门牌」的顺序逐个试写，并用门店距离做交叉校验。
        """
        # 缓存键必须带上 city：同一个门牌在不同城市会解析到完全不同的地方
        key = f"geo:{city.strip()}|{address.strip()}"
        cached = self.quota.cache_get(key)
        if cached and cached.get("lat") is not None and cached.get("lng") is not None:
            point = (float(cached["lat"]), float(cached["lng"]))
            if self._accept(point, near, expected_m):
                return point

        for candidate in _address_candidates(address, city):
            lat, lng = self._geocode_once(candidate, city)
            if lat is None or lng is None:
                continue
            point = (float(lat), float(lng))
            if not self._accept(point, near, expected_m):
                continue  # 同名异地，换下一种写法再试（命中率很低，不会浪费多少配额）
            self.quota.cache_put(key, {"lat": point[0], "lng": point[1]})
            return point
        return None

    @staticmethod
    def _accept(
        point: Tuple[float, float],
        near: Optional[Tuple[float, float]],
        expected_m: Optional[float],
    ) -> bool:
        """给一个解析结果做可信度校验（拿不到参照时默认可信）。"""
        if not near or not expected_m:
            return True
        return _is_plausible(haversine_m(near, point), expected_m)

    def _geocode_once(
        self, address: str, city: str = ""
    ) -> Tuple[Optional[float], Optional[float]]:
        result = self._get("geocoder/v1/", {"address": address, "region": city} if city else {"address": address})
        location = ((result or {}).get("location") or {})
        lat, lng = location.get("lat"), location.get("lng")
        if lat is None or lng is None:
            # 腾讯配额按服务分别统计：geocoder 用完时 place 搜索可能还有额度，
            # 这里用地点搜索顶上（**可选**，不分配也不影响主流程）。
            primary_reason = self.blocked_reason
            lat, lng = self._geocode_via_place(address, city)
            if lat is None and primary_reason:
                # 兜底也没成功时，保留主路径的原因，别让 place 的 121 刷掉它
                self.blocked_reason = primary_reason
        if lat is None or lng is None:
            return None, None
        return float(lat), float(lng)

    def reverse_geocode(self, point: Tuple[float, float]) -> Optional[str]:
        """逆地址解析（坐标 → 文字地址），带 7 天缓存。

        `query-nearby-stores` 不给门店经纬度，用户的手机定位也只有一对坐标，
        这一步把坐标翻成「示例市示例区示例路132号」这样的可读位置显示在界面上。
        """
        if not self.available:
            return None
        lat, lng = _round_coord(point)
        key = f"rgeo:{lat},{lng}"
        cached = self.quota.cache_get(key)
        if cached and cached.get("address"):
            return str(cached["address"])
        result = self._get("geocoder/v1/", {"location": f"{point[0]},{point[1]}"})
        address = str((result or {}).get("address") or "").strip()
        if not address:
            comp = ((result or {}).get("address_component") or {})
            address = "".join(
                str(comp.get(field) or "")
                for field in ("province", "city", "district", "street", "street_number")
            ).strip()
        if not address:
            return None
        self.quota.cache_put(key, {"address": address})
        return address

    def _geocode_via_place(
        self, address: str, city: str = ""
    ) -> Tuple[Optional[float], Optional[float]]:
        """用 place/v1/search 兜底解析坐标（geocoder 触配额时仍可能有额度）。"""
        params: Dict[str, Any] = {"keyword": address, "page_size": 1}
        if city:
            params["boundary"] = f"region({city},0)"
        result = self._get("place/v1/search/", params)
        for item in (result or {}).get("data") or []:
            location = item.get("location") or {}
            if location.get("lat") is not None and location.get("lng") is not None:
                return float(location["lat"]), float(location["lng"])
        return None, None

    def route(
        self,
        origin: Tuple[float, float],
        dest: Tuple[float, float],
        mode: str = "walk",
    ) -> Optional[Route]:
        if not self.available:
            return None
        o, d = _round_coord(origin), _round_coord(dest)
        cache_key = f"route:{o[0]},{o[1]}->{d[0]},{d[1]}:{mode}"
        cached = self.quota.cache_get(cache_key)
        if cached:
            hit = _route_from_json(cached)
            if hit:
                hit.note = "来自本地缓存，未消耗地图配额"
                return hit

        path = MODE_PATHS.get(mode, "walking")
        params = {
            "from": f"{origin[0]},{origin[1]}",
            "to": f"{dest[0]},{dest[1]}",
        }
        if mode == "drive":
            params["policy"] = "LEAST_TIME"
        result = self._get(f"direction/v1/{path}/", params)
        routes = (result or {}).get("routes") or []
        if not routes:
            return None
        first = routes[0]
        distance = float(first.get("distance") or 0)
        duration = float(first.get("duration") or 0)
        # 腾讯步行/骑行 duration 单位为分钟；驾车为分钟。若明显偏大则按秒处理。
        minutes = duration / 60 if duration > 600 else duration
        if minutes <= 0 and distance:
            minutes = distance / SPEED_M_PER_MIN.get(mode, 75.0)

        steps: List[str] = []
        polyline: List[Tuple[float, float]] = []
        for step in first.get("steps") or []:
            instruction = step.get("instruction")
            if instruction:
                steps.append(str(instruction))
            polyline.extend(decode_polyline(step.get("polyline") or []))
        if not polyline:
            polyline = [origin, dest]

        route = Route(
            distance_m=round(distance, 1),
            minutes=round(minutes, 1),
            mode=mode,
            source="tencent",
            steps=steps[:8],
            polyline=polyline,
            origin=origin,
            dest=dest,
        )
        self.quota.cache_put(cache_key, _route_to_json(route))
        return route


def estimate_route(distance_m: float, mode: str = "walk") -> Route:
    """无 Key 时的本地估算：按绕行系数还原道路距离。"""
    road = distance_m * DETOUR_FACTOR
    minutes = road / SPEED_M_PER_MIN.get(mode, 75.0)
    return Route(distance_m=round(road, 1), minutes=round(minutes, 1), mode=mode, source="estimate")


def _is_plausible(route_m: float, expected_m: Optional[float]) -> bool:
    """判断真实路线距离是否可信。

    门店地址反查存在「同名异地」风险（实测「南京西路1601号」被解析到江苏南京），
    会算出几百公里的荒谬路线。这里用 MCP 给出的门店距离做交叉校验：
    真实路线超过预期 5 倍且绝对值 > 15 km 时判定为不可信。
    """
    if not expected_m or expected_m <= 0:
        return route_m <= 100_000  # 无参照时只挡明显跨城的错误
    return route_m <= max(expected_m * 5, 15_000)


def route_between(
    origin: Optional[Tuple[float, float]],
    dest: Optional[Tuple[float, float]],
    fallback_distance_m: Optional[float],
    mode: str = "walk",
    geo: Optional[TencentGeo] = None,
) -> Route:
    """优先真实路径规划，失败则按 MCP 返回的门店距离估算。

    地图不可用时绝不抛异常：一律降级为估算路线，并在 route.note 里写明原因
    （未配 Key / 已触配额上限 / 服务不可用），供界面如实标注。
    """
    reason = ""
    if geo and not geo.available:
        reason = "未配置 T2M_TMAP_KEY，按距离估算"
    elif geo and not origin:
        # 起点缺失：地址解析未开通时没法把门牌文字变成坐标，
        # 手机定位是此时唯一能拿到真实路线的途径，必须明说。
        reason = "未拿到起点坐标（地址解析额度未开通时，请用「获取当前位置」按钮），按门店距离估算"
    elif geo and not dest:
        reason = getattr(geo, "blocked_reason", "") or "门店坐标缺失，按门店距离估算"
    elif geo and origin and dest:
        route = geo.route(origin, dest, mode)
        if route and _is_plausible(route.distance_m, fallback_distance_m):
            return route
        if route:
            # 真实路线离谱地长（实测：地址被反查到同名异地，如「南京西路」→ 江苏南京）
            reason = (
                f"路线异常（{route.distance_m / 1000:.1f} km，与门店距离不符，疑地址重名），按距离估算"
            )
        else:
            reason = getattr(geo, "blocked_reason", "") or "地图服务暂不可用，按距离估算"
    elif geo:
        # 起点或终点拿不到（如门店地址反查失败），把腾讯给出的原因透传出去
        reason = getattr(geo, "blocked_reason", "")
    distance = fallback_distance_m
    if distance is None and origin and dest:
        distance = haversine_m(origin, dest)
    if distance is None:
        distance = 0.0
    route = estimate_route(distance, mode)
    route.origin, route.dest = origin, dest
    route.note = reason
    return route


_MAP_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@</title>
<style>
  html, body { margin: 0; padding: 0; height: 100%; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
  #map { width: 100%; height: 100vh; }
  .panel {
    position: absolute; left: 16px; top: 16px; z-index: 10;
    background: rgba(255,255,255,.96); border-radius: 12px; padding: 14px 16px;
    box-shadow: 0 6px 22px rgba(0,0,0,.14); min-width: 240px; max-width: 320px;
    color: #1b1b1b; font-size: 13px; line-height: 1.6;
  }
  .panel h1 { margin: 0 0 8px; font-size: 15px; }
  .row { display: flex; justify-content: space-between; gap: 12px; }
  .k { color: #6b6b6b; }
  .v { font-weight: 600; }
  .kcal { color: #c8102e; font-weight: 700; font-size: 18px; }
  .badge { display:inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px;
           background:#fff1f0; color:#c8102e; margin-top:6px; }
  .tip { margin-top: 8px; color: #8a8a8a; font-size: 11px; line-height: 1.5; }
</style>
@@SECURITY@@
<script src="@@SDK@@"></script>
</head>
<body>
<div id="map"></div>
<div class="panel">
  <h1>@@STORE@@</h1>
  <div class="row"><span class="k">起点</span><span class="v">@@ORIGIN@@</span></div>
  <div class="row"><span class="k">路程</span><span class="v">@@DISTANCE@@ · @@MINUTES@@ 分钟</span></div>
  <div class="row"><span class="k">方式</span><span class="v">@@MODE@@</span></div>
  <div class="row"><span class="k">预计消耗</span><span class="kcal">@@KCAL@@ kcal</span></div>
  @@BADGE@@
  <div class="tip">@@TIP@@</div>
</div>
<script>
var routePoints = @@POLYLINE@@;
var center = new TMap.LatLng(@@LAT@@, @@LNG@@);
var map = new TMap.Map('map', { zoom: 15, center: center });

var markerStyle = new TMap.MarkerStyle({
  width: 24, height: 32,
  anchor: { x: 12, y: 32 },
  src: 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="32"><path fill="#c8102e" d="M12 0C5.4 0 0 5.4 0 12c0 8 12 20 12 20s12-12 12-20c0-6.6-5.4-12-12-12z"/><circle cx="12" cy="12" r="4.5" fill="#fff"/></svg>')
});

var geometries = [{ id: 'store', styleId: 'default', position: center }];
if (routePoints.length > 1) {
  var start = routePoints[0];
  geometries.push({ id: 'start', styleId: 'default', position: new TMap.LatLng(start.lat, start.lng) });
}
new TMap.MultiMarker({ map: map, styles: { default: markerStyle }, geometries: geometries });

if (routePoints.length > 1) {
  var path = routePoints.map(function (p) { return new TMap.LatLng(p.lat, p.lng); });
  new TMap.MultiPolyline({
    map: map,
    styles: {
      route: new TMap.PolylineStyle({ color: '#c8102e', width: 6, lineCap: 'round', showArrow: true })
    },
    geometries: [{ id: 'route', styleId: 'route', paths: path }]
  });
  map.fitBounds(new TMap.LatLngBounds(path), { padding: 90 });
}
</script>
</body>
</html>
"""


def render_map_html(
    store_name: str,
    lat: float,
    lng: float,
    route: Optional[Route] = None,
    origin_label: str = "我的位置",
    weight_kg: float = 65.0,
    api_key: str = "",
    tip: str = "",
) -> str:
    """生成单文件地图页。

    api_key 为空时使用腾讯 GL JS 的密钥代理模式（前端无 Key）；
    传入用户自备 Key 时，SDK 加上 key 参数（仅本地生成，不进仓库）。
    """
    if api_key:
        security = "<!-- 使用你自备的腾讯位置服务 Key（本地生成，请勿提交到仓库） -->"
        sdk = f"https://map.qq.com/api/gljs?v=1.exp&key={api_key}"
    else:
        security = (
            "<!-- 密钥代理模式：无需在前端暴露 Key，由 WorkBuddy 运行时注入 -->\n"
            "<script type=\"text/javascript\">\n"
            "  window._TMapSecurityConfig = {\n"
            "    serviceHost: 'http://127.0.0.1:__WB_HTTP_PORT__/_TMapService/_wbt/__WB_TMAP_SECRET__',\n"
            "  };\n"
            "</script>"
        )
        sdk = "https://map.qq.com/api/gljs?v=1.exp"

    points = []
    if route and route.polyline:
        points = [{"lat": round(p[0], 6), "lng": round(p[1], 6)} for p in route.polyline]

    kcal = route.calories(weight_kg) if route else 0.0
    badge = ""
    if route and route.source == "estimate":
        badge = '<span class="badge">距离为估算值，配置地图 Key 可得真实路线</span>'
    elif route:
        badge = '<span class="badge">路线来自腾讯位置服务</span>'

    replacements = {
        "@@TITLE@@": f"{store_name} · 通勤路线",
        "@@SECURITY@@": security,
        "@@SDK@@": sdk,
        "@@STORE@@": store_name,
        "@@ORIGIN@@": origin_label,
        "@@DISTANCE@@": route.distance_text if route else "—",
        "@@MINUTES@@": f"{route.minutes:g}" if route else "—",
        "@@MODE@@": route.mode_label if route else "—",
        "@@KCAL@@": f"{kcal:.0f}",
        "@@BADGE@@": badge,
        "@@TIP@@": tip or "地图底图由腾讯位置服务提供；通勤消耗按 MET 公式粗算，仅供参考。",
        "@@POLYLINE@@": json.dumps(points, ensure_ascii=False),
        "@@LAT@@": f"{lat:.6f}",
        "@@LNG@@": f"{lng:.6f}",
    }
    html = _MAP_TEMPLATE
    for token, value in replacements.items():
        html = html.replace(token, value)
    return html
