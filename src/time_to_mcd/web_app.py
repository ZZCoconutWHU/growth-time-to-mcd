"""本地 Web 服务：让像素前端从浏览器直接读写 SQLite。

`python run.py web` 会启动它。提供：
  - 静态托管 web/（index.html / app.js / style.css / stats.js ...）
  - GET  /api/stats          成长数据快照（同 export_web）
  - GET  /api/words          词库（游戏在本地组题，需要全量）
  - GET  /api/now            麦当劳服务端时间 + 按时间推荐的餐次
  - POST /api/profile        保存身高体重并算 BMI + 国标分餐目标
  - POST /api/play           记录一局背词（写回 SM-2 间隔与专注时长）
  - POST /api/meal           快捷记录一餐（可带 commute 一起记通勤消耗）
  - POST /api/commute        单独记一次步行/骑行的消耗
  - GET  /api/sync/status    同步面板：库内条数、上次同步时间、手机该访问的地址
  - GET  /api/sync/pull      导出一份成长档案快照（给另一台设备导入或当备份）
  - POST /api/sync/push      手机补传离线时攒的记录 / 导入一份快照（按 uid 去重）

手机带出门会断网：页面转离线模式把记录排进浏览器队列，回到同一个 WiFi
后自动补传；实在见不到面也能「导出 JSON → 传文件 → 导入」。

零第三方依赖，只用标准库。生产部署请加反向代理与鉴权。
"""

from __future__ import annotations

import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "web"

# Web 服务固定读写同一个库，避免「写入 records.db、读取 demo.db」的分裂
_DB_PATH = os.environ.get("T2M_DB") or str(ROOT / "data" / "records.db")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _store():
    from .store import RecordStore  # 延迟导入，避免循环

    return RecordStore(Path(_DB_PATH))


def _profile_from(body: Dict[str, Any]):
    from .store import Profile

    return Profile(
        height_cm=float(body.get("height_cm") or 0),
        weight_kg=float(body.get("weight_kg") or 0),
        age=int(body.get("age") or 0),
        sex=str(body.get("sex") or "male"),
        activity=str(body.get("activity") or "light"),
        goal=str(body.get("goal") or "maintain"),
    )


def _mock_mode(settings: Any) -> bool:
    """是否强制演示模式。

    两种情况走 mock：没配 Token（自然演示），或显式 `--demo`（T2M_MOCK=1）。
    --demo 的语义是「完整演示」：连 MCP 订单也用假的，
    否则演示时会把本人账号里的真实订单/门店暴露在投影上。
    """
    return not settings.token or os.environ.get("T2M_MOCK") == "1"


def _service():
    """构建业务 service。

    必须走 `config.load_settings()`：它会加载项目根目录的 `.env`。
    若直接用 `os.environ.get("MCD_MCP_TOKEN")`，写在 `.env` 里的 Token 就读不到了。
    没配 Token 时自动退回 mock 演示，保证前端任何环境下都有内容可展示。
    """
    from .config import load_settings
    from .service import build_service

    settings = load_settings()
    service, _ = build_service(settings, mock=_mock_mode(settings), require_auth=False)
    return service, settings


def _parse_origin(raw: Any) -> Optional[Tuple[float, float]]:
    """前端定位结果 → (lat, lng)；非法值一律当「未提供」，退回估算路线。"""
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    try:
        lat, lng = float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        return None
    if not (3 < lat < 54 and 73 < lng < 136):  # 中国境内粗校验，防脏数据打地图
        return None
    return lat, lng


def _geo_quota(service: Any) -> Dict[str, Any]:
    """地图调用配额快照（每分钟 5 次 / 每日 100 次）。拿不到就返回不可用标记。"""
    geo = getattr(service, "geo", None)
    snap = getattr(geo, "quota_snapshot", None)
    try:
        return snap() if callable(snap) else {"available": False}
    except Exception:  # noqa: BLE001
        return {"available": False}


def _order_url() -> str:
    """官方下单入口（麦当劳网上订餐平台），可用 T2M_ORDER_URL 覆盖。

    本项目不调用 create-order —— 下单始终交给麦当劳官方渠道完成，零资金风险。
    """
    return (os.environ.get("T2M_ORDER_URL") or "https://mcd.cn/").strip() or "https://mcd.cn/"


_LAN_IP_CACHE: Dict[str, Any] = {}


def _os_local_ips() -> List[str]:
    """问操作系统要一遍本机 IP（ipconfig / ip / ifconfig），只要私有地址。

    装了代理软件时主机名解析常常被虚拟网卡顶掉，只有枚举网卡才拿得到
    WiFi 那张卡的 192.168.x.x。
    """
    import re
    import subprocess

    out = ""
    for cmd in (["ipconfig"], ["ip", "-4", "addr"], ["ifconfig"]):
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=3)
            text = (proc.stdout or b"").decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001 命令不存在就直接换下一个
            continue
        if text:
            out = text
            break
    if not out:
        return []
    # Windows：IPv4 地址 / IPv4 Address 后面跟的那个才是本机地址（网关、DNS 不算）
    found = re.findall(r"IPv4[^0-9]{0,40}(\d+\.\d+\.\d+\.\d+)", out)
    if not found:
        # Linux / macOS：inet 192.168.1.113/24
        found = re.findall(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
    return [ip for ip in found if not ip.startswith("127.") and not ip.startswith("169.254.")]


def _lan_ip() -> str:
    """挑一个手机能访问到的本机 IP（私有地址优先，避开代理虚拟网卡）。"""
    import socket
    import time

    now = time.time()
    if _LAN_IP_CACHE.get("ip") and now - float(_LAN_IP_CACHE.get("at", 0)) < 60:
        return str(_LAN_IP_CACHE["ip"])

    candidates: List[str] = []
    try:
        candidates += _os_local_ips()
    except Exception:  # noqa: BLE001
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                candidates.append(ip)
    except Exception:  # noqa: BLE001
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("223.5.5.5", 80))  # 不发包，只为问系统「走哪张网卡出去」
        ip = probe.getsockname()[0]
        probe.close()
        if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
            candidates.append(ip)
    except Exception:  # noqa: BLE001
        pass

    def rank(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172.") and 16 <= int((ip.split(".") + ["0"])[1] or 0) <= 31:
            return 2
        return 3

    unique: List[str] = []
    for ip in candidates:
        if ip not in unique:
            unique.append(ip)
    unique.sort(key=rank)
    picked = unique[0] if unique else ""
    _LAN_IP_CACHE["ip"] = picked
    _LAN_IP_CACHE["at"] = now
    return picked


def _lan_url(server_address: Any = None) -> str:
    """本机在局域网里的访问地址，用来告诉手机「连同一个 WiFi 后打开这个」。"""
    port = 8000
    try:
        port = int(server_address[1]) if server_address else 8000
    except (TypeError, ValueError, IndexError):
        port = 8000
    ip = _lan_ip()
    return f"http://{ip}:{port}/" if ip else ""


def _sync_lan_url(server_address: Any = None) -> str:
    """同步面板展示用的局域网地址。

    演示模式（T2M_MOCK=1 / 未配 Token）下不把真实内网 IP 投到屏幕上，
    返回占位写法——截图、投屏发到哪都不会带出家里网络的任何线索。
    """
    from .config import load_settings

    if _mock_mode(load_settings()):
        try:
            port = int(server_address[1]) if server_address else 8000
        except (TypeError, ValueError, IndexError):
            port = 8000
        return f"http://<电脑局域网IP>:{port}/"
    return _lan_url(server_address)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def _send(self, code: int, payload: Any, ctype: str = "application/json; charset=utf-8"):
        data = payload if isinstance(payload, (str, bytes)) else json.dumps(payload, ensure_ascii=False)
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _api_stats(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import export_web  # type: ignore

        return export_web.collect(Path(_DB_PATH))

    def _api_words(self) -> List[Dict[str, Any]]:
        store = _store()
        rows = store.sample_words(1000)
        if rows:
            return rows
        # 词库表为空时退回 JSON 文件
        bank = ROOT / "data" / "wordbank.json"
        if bank.is_file():
            return json.loads(bank.read_text(encoding="utf-8")).get("words", [])
        return []

    def _api_order(self) -> Dict[str, Any]:
        """最新订单状态：复用 service.order_detail()（= MCP 4.2.13 查询订单详情）。

        有 MCD_MCP_TOKEN 走真实 MCP；否则退回 mock 演示订单，并在返回里标 demo=true，
        前端据此提示「未连接 MCP」，绝不把演示数据伪装成真实订单。
        """
        from time_to_mcd.wait import progress_ratio

        # token 单独从配置读（会加载 .env），不依赖 service 是否构建成功：
        # 这样「没配 Token（演示）」和「配了但拉取失败」在前端能区分开。
        from .config import load_settings

        settings = load_settings()
        mock = _mock_mode(settings)
        token = "" if mock else settings.token
        try:
            service, _ = _service()
            order = service.order_detail()
            if not order:
                return {"connected": bool(token), "demo": mock, "order": None}
            eta, plan = service.wait_state(order, travel_min=10.0)
            return {
                "connected": bool(token),
                "demo": mock,
                "order": {
                    "order_id": order.order_id,
                    # 下单时间：前端靠它判断「这是不是刚下的那一单」
                    "created_at": order.created_at.isoformat(timespec="seconds") if order.created_at else "",
                    "status_text": order.status_text,
                    "stage_label": order.stage_label,
                    "mode_label": order.mode_label,
                    "store_name": order.store_name,
                    "pickup_code": order.pickup_code,
                    "amount": order.amount,
                    "items": order.items,
                    "progress": round(progress_ratio(order) * 100),
                    "is_final": order.is_final,
                },
                "eta": {
                    "remaining_min": eta.remaining_min,
                    "confidence": eta.confidence,
                    "basis": eta.basis,
                },
                "plan": {"advice": plan.advice, "tone": plan.tone},
            }
        except Exception as exc:  # noqa: BLE001
            return {"connected": bool(token), "demo": not token, "order": None, "error": str(exc)}

    def _api_config(self) -> Dict[str, Any]:
        """前端启动时拉取的少量配置：官方下单入口、是否接了真实 MCP、有无地图 Key。"""
        from .config import load_settings

        settings = load_settings()
        return {
            "order_url": _order_url(),
            "connected": bool(settings.token) and not _mock_mode(settings),
            "demo": _mock_mode(settings),
            "has_tmap_key": bool(settings.tmap_key),
        }

    def _api_now(self) -> Dict[str, Any]:
        """麦当劳服务端时间 + 按时间推荐的餐次（进「这一餐吃什么」时的默认值）。

        时间优先取 MCP `now-time-info`；拿不到就退回本机时间并如实标注 source=local，
        绝不把本机时间说成服务端时间。
        """
        try:
            service, settings = _service()
            data = service.current_meal()
            data["connected"] = bool(settings.token) and not _mock_mode(settings)
            return data
        except Exception as exc:  # noqa: BLE001 时间拿不到也要能继续用页面
            from datetime import datetime

            from .coach import MEAL_LABEL, meal_of_time, meal_window_text

            moment = datetime.now()
            meal = meal_of_time(moment)
            return {
                "meal": meal,
                "meal_label": MEAL_LABEL.get(meal, meal),
                "time": moment.strftime("%Y-%m-%d %H:%M"),
                "hour": moment.hour,
                "source": "local",
                "window": meal_window_text(meal),
                "connected": False,
                "error": str(exc),
            }

    def _api_history(self) -> Dict[str, Any]:
        """历史订单 → 给「记录」屏回填真实吃过的东西（`order-list` + 营养库匹配）。

        只读取，不写库。是否「已记入档案」由 meals.ref_id 判断，
        避免同一单被重复记录。
        """
        service, settings = _service()
        limit = 5
        try:
            orders = service.order_history(limit=limit)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "orders": [], "demo": _mock_mode(settings)}

        try:
            logged = service.db.logged_ref_ids()
        except Exception:  # noqa: BLE001
            logged = set()

        from .coach import MEAL_LABEL, meal_of_time, meal_window_text

        rows = []
        for order in orders:
            nutrition = service.order_nutrition(order)
            # 这一单算哪一餐：跟「出发」屏用的是同一条规则（coach.MEAL_WINDOWS），
            # 不再各写一份判断，避免两处口径不一致。
            meal = meal_of_time(order.created_at) if order.created_at else ""
            rows.append(
                {
                    "order_id": order.order_id,
                    "store": order.store_name,
                    "time": order.created_at.strftime("%Y-%m-%d %H:%M") if order.created_at else "",
                    "meal": meal,
                    "meal_label": MEAL_LABEL.get(meal, meal),
                    "meal_window": meal_window_text(meal) if meal else "",
                    "status_text": order.status_text,
                    "stage_label": order.stage_label,
                    "mode_label": order.mode_label,
                    "amount": order.amount,
                    "items": [
                        {
                            "name": str(i.get("name") or i.get("productName") or ""),
                            "qty": i.get("qty") or i.get("quantity") or 1,
                            "price": i.get("price") or None,
                        }
                        for i in (order.items or [])
                    ],
                    "nutrition": nutrition,
                    "logged": order.order_id in logged,
                }
            )
        return {
            "orders": rows,
            "connected": bool(settings.token) and not _mock_mode(settings),
            "demo": _mock_mode(settings),
        }

    def _api_reverse(self) -> Dict[str, Any]:
        """坐标 → 地址（逆地址解析），给「获取当前位置」按钮把地名填回输入框。"""
        from .geo import TencentGeo
        from . import config as cfg

        lat = self.query.get("lat", [""])[0]
        lng = self.query.get("lng", [""])[0]
        try:
            point = (float(lat), float(lng))
        except (TypeError, ValueError):
            return {"error": "lat / lng 不合法", "address": ""}
        settings = cfg.load_settings()
        geo = TencentGeo(settings.tmap_key) if settings.tmap_key else None
        if not geo or not geo.available:
            return {"address": "", "coords": list(point), "error": "未配置 T2M_TMAP_KEY，无法把坐标翻成地址"}
        address = ""
        try:
            address = geo.reverse_geocode(point) or ""
        except Exception as exc:  # noqa: BLE001
            return {"address": "", "coords": list(point), "error": str(exc)}
        return {
            "address": address,
            "coords": list(point),
            "blocked_reason": getattr(geo, "blocked_reason", ""),
        }

    # ---------------------------------------------------- 手机 ↔ 电脑同步
    # 手机带出门会断网，页面转离线模式把记录排进浏览器队列；
    # 回到同一个 WiFi 后自动补传，每条记录带 uid，重传只落一次。
    def _api_sync_status(self) -> Dict[str, Any]:
        """同步面板要的信息：库里有多少条、上次同步时间、手机该访问哪个地址。

        前端也靠这个接口探测「电脑上的服务在不在」——探测成功就开始补传。
        """
        from datetime import datetime

        store = _store()
        meta = store.all_meta()
        return {
            "ok": True,
            "server_time": datetime.now().isoformat(timespec="seconds"),
            "last_sync_at": meta.get("last_sync_at", ""),
            "last_device": meta.get("last_device", ""),
            "stats": store.stats(),
            # 演示模式不把真实内网 IP 投到屏幕/截图上
            "lan_url": _sync_lan_url(getattr(getattr(self, "server", None), "server_address", None)),
            "db_path": str(store.path),
        }

    def _api_sync_pull(self) -> Dict[str, Any]:
        """导出一份成长档案快照（给另一台设备导入，或当备份存下来）。"""
        since = (self.query.get("since", [""])[0] or "").strip()
        store = _store()
        snap = store.sync_snapshot(since=since or None)
        snap["lan_url"] = _sync_lan_url(getattr(getattr(self, "server", None), "server_address", None))
        return snap

    def _post_sync_push(self):
        """接收手机补传的一批记录（或一整份快照）。

        两种载荷：
          - {"device": "...", "ops": [{"type","uid","ts","payload"}, ...]}  增量补传
          - {"device": "...", "snapshot": {...}}                            整包导入（JSON 搬家）
        两条路都按 uid 去重，重复上传不会把卡路里或单词数算两遍。
        """
        from datetime import datetime

        from . import sync

        body = self._body()
        device = str(body.get("device") or "")
        store = _store()
        stamp = datetime.now().isoformat(timespec="seconds")

        if isinstance(body.get("snapshot"), dict):
            merged = store.merge_snapshot(body["snapshot"])
            store.set_meta("last_device", device or "import")
            store.set_meta("last_sync_at", stamp)
            return self._send(200, {"ok": True, "mode": "snapshot", "merged": merged, "stats": store.stats()})

        ops = body.get("ops") or []
        if not isinstance(ops, list) or not ops:
            return self._send(400, {"error": "ops 为空，或缺少 snapshot"})
        result = sync.apply_ops(store, ops, device=device, resolve_order=self._resolve_order)
        result["ok"] = not result["errors"]
        result["mode"] = "ops"
        result["stats"] = store.stats()
        return self._send(200, result)

    @staticmethod
    def _resolve_order(order_id: str) -> Optional[Dict[str, Any]]:
        """按订单号补出真实营养——只有在线时才做得到（补传里带 order_id 时用）。"""
        try:
            service, _ = _service()
            order = service.order_detail(order_id) or _find_order(service, order_id)
            if not order:
                return None
            nutrition = service.order_nutrition(order)
            if nutrition["matched"] == 0:
                return None
            from .coach import meal_of_time

            meal_type = meal_of_time(order.created_at) if order.created_at else "lunch"
            return {
                "items": [
                    {
                        "name": str(i.get("name") or i.get("productName") or ""),
                        "qty": i.get("qty") or i.get("quantity") or 1,
                    }
                    for i in (order.items or [])
                ],
                "kcal": nutrition["kcal"],
                "protein": nutrition["protein"],
                "fat": nutrition["fat"],
                "carb": nutrition["carb"],
                "sodium": nutrition["sodium"],
                "store": order.store_name,
                "meal_type": meal_type,
                "target_kcal": service.targets(meal=meal_type).kcal,
                "ts": order.created_at.isoformat(timespec="seconds") if order.created_at else "",
            }
        except Exception:  # noqa: BLE001
            return None

    def _api_stores(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """附近门店 + 通勤消耗（网页端「出发」屏，对应 CLI `near`）。"""
        address = str(body.get("address") or "").strip()
        if not address:
            return {"error": "请先填写地址或地标", "stores": []}
        mode = str(body.get("mode") or "walk")
        if mode not in ("walk", "bike"):
            mode = "walk"
        need = {str(x) for x in (body.get("need") or [])}
        limit = max(1, min(10, int(body.get("limit") or 3)))
        origin = _parse_origin(body.get("origin"))

        service, settings = _service()
        try:
            stores = service.find_stores(
                address,
                need=need,
                max_distance_m=body.get("max_distance_m"),
                limit=limit,
                prefer_delivery="delivery" in need,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "stores": [], "demo": _mock_mode(settings)}

        weight = service.profile_or_default().weight_kg
        rows = []
        for store in stores:
            commute = None
            try:
                route = service.commute(store, mode=mode, origin=origin)
                commute = {
                    "minutes": round(route.minutes, 1),
                    "distance_m": round(route.distance_m, 1),
                    "kcal": round(route.calories(weight), 1),
                    "mode_label": route.mode_label,
                    "source": route.source,  # tencent=真实路线 / estimate=估算
                    "note": route.note,  # 降级原因或缓存说明，如实展示
                }
            except Exception:  # noqa: BLE001
                commute = None
            rows.append(
                {
                    "code": store.code,
                    "name": store.name,
                    "address": store.address,
                    "hours": store.hours,
                    "status": store.status,
                    "is_open": store.is_open,
                    "distance_m": store.distance_m,
                    "distance_text": store.distance_text,
                    "services": sorted(store.services),
                    "commute": commute,
                }
            )
        # 有 GPS 坐标时用逆地址解析显示「你在 XX 附近」，让定位结果有实感
        origin_label = ""
        if origin:
            try:
                geo = getattr(service, "geo", None)
                if geo and getattr(geo, "available", False):
                    origin_label = geo.reverse_geocode(origin) or ""
            except Exception:  # noqa: BLE001
                origin_label = ""

        return {
            "address": address,
            "mode": mode,
            "weight_kg": weight,
            "connected": bool(settings.token) and not _mock_mode(settings),
            "demo": _mock_mode(settings),
            "stores": rows,
            "order_url": _order_url(),
            "origin": list(origin) if origin else None,
            "origin_label": origin_label,
            "quota": _geo_quota(service),
        }

    def _api_preorder(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """按身体数据推荐这一餐（网页端「出发」屏，对应 CLI `preorder`）。

        meal 传 auto/空 时按麦当劳服务端时间自动落到早餐/午餐/晚餐/加餐；
        用户手动选过餐次就传具体值，此时以用户选择为准。
        """
        raw_meal = str(body.get("meal") or "").strip().lower()
        limit = max(1, min(10, int(body.get("limit") or 3)))

        service, settings = _service()
        now_info = service.current_meal()
        auto = raw_meal in ("", "auto")
        meal = now_info["meal"] if auto else raw_meal
        profile = service.profile_or_default()
        try:
            targets, combos, catalog_size = service.recommend(profile, meal=meal, limit=limit)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "combos": [], "demo": _mock_mode(settings)}

        from . import coach

        return {
            "meal": meal,
            "auto": auto,
            "now": now_info,
            "catalog_size": catalog_size,
            "connected": bool(settings.token) and not _mock_mode(settings),
            "demo": _mock_mode(settings),
            "profile": coach.profile_summary(profile),
            "targets": targets.__dict__,
            "combos": [
                {
                    "items": [food.name for food in combo.items],
                    "kcal": round(combo.kcal, 1),
                    "protein": round(combo.protein, 1),
                    "fat": round(combo.fat, 1),
                    "carb": round(combo.carb, 1),
                    "sodium": round(combo.sodium, 1),
                    "score": round(combo.score, 1),
                    "reasons": combo.reasons,
                    "gaps": combo.gaps,
                }
                for combo in combos
            ],
            "order_url": _order_url(),
        }

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        self.query = parse_qs(parsed.query)
        if path.startswith("/api/"):
            try:
                if path == "/api/stats":
                    return self._send(200, self._api_stats())
                if path == "/api/words":
                    return self._send(200, self._api_words())
                if path == "/api/order":
                    return self._send(200, self._api_order())
                if path == "/api/now":
                    return self._send(200, self._api_now())
                if path == "/api/history":
                    return self._send(200, self._api_history())
                if path == "/api/config":
                    return self._send(200, self._api_config())
                if path == "/api/reverse":
                    return self._send(200, self._api_reverse())
                if path == "/api/sync/status":
                    return self._send(200, self._api_sync_status())
                if path == "/api/sync/pull":
                    return self._send(200, self._api_sync_pull())
            except Exception as exc:  # noqa: BLE001
                return self._send(500, {"error": str(exc)})
            return self._send(404, {"error": "unknown api"})

        # 静态文件：阻止越界
        if ".." in path or path.startswith("/."):
            return self._send(403, {"error": "forbidden"})
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/stores":
                return self._send(200, self._api_stores(self._body()))
            if path == "/api/preorder":
                return self._send(200, self._api_preorder(self._body()))
            if path == "/api/profile":
                return self._post_profile()
            if path == "/api/play":
                return self._post_play()
            if path == "/api/meal":
                return self._post_meal()
            if path == "/api/commute":
                return self._post_commute()
            if path == "/api/sync/push":
                return self._post_sync_push()
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"error": str(exc)})
        return self._send(404, {"error": "unknown api"})

    def _post_profile(self):
        body = self._body()
        if not body.get("height_cm") or not body.get("weight_kg") or not body.get("age"):
            return self._send(400, {"error": "height_cm / weight_kg / age 为必填"})
        store = _store()
        profile = _profile_from(body)
        store.save_profile(profile)

        from .coach import meal_plan

        plan = meal_plan(profile)
        return self._send(200, {"profile": store.get_profile().__dict__, "plan": plan})

    def _post_play(self):
        """记一局背词。

        uid 由前端生成：手机离线时先排队、回来连上 WiFi 再补传，
        同一个 uid 重传只会落一次，否则重试会把同一批单词的答对数算两遍。
        """
        body = self._body()
        words = body.get("words") or []
        if not words:
            return self._send(400, {"error": "words 不能为空"})
        from . import sync

        uid = str(body.get("uid") or "")
        ok = sync.apply_play(_store(), body, uid, None)
        correct = sum(1 for w in words if (w or {}).get("correct"))
        return self._send(200, {"ok": True, "words": len(words), "correct": correct, "duplicate": not ok})

    def _post_meal(self):
        """记录一餐。

        两种来源：
          - 手填：前端传 items（带 kcal/protein/...）
          - 历史订单：只传 order_id，由 MCP 明细 + 营养库算出真实营养，
            并用 ref_id 标记，避免同一单重复记入。
        """
        body = self._body()
        order_id = str(body.get("order_id") or "").strip()
        uid = str(body.get("uid") or "")
        store = _store()

        # 手机补传：同一条记录重传直接跳过，别把一餐记两遍
        if uid and store.uid_rowid("meals", uid):
            return self._send(200, {"ok": True, "duplicate": True})

        # 通勤消耗：下单后前端每 2 分钟轮询一次，一旦出现新订单就把这一趟的
        # 距离/时长/卡路里记进 commutes（ref_id=订单号，同一单只记一次）。
        commute_logged = self._log_commute(store, body.get("commute"), order_id, uid=(uid + ":c") if uid else "")

        if order_id:
            service, settings = _service()
            order = service.order_detail(order_id) or _find_order(service, order_id)
            if not order:
                return self._send(404, {"error": f"没找到订单 {order_id}"})
            nutrition = service.order_nutrition(order)
            if nutrition["matched"] == 0:
                return self._send(
                    400,
                    {"error": "这一单的商品在营养库里匹配不到，先用下面的表单手动记录"},
                )
            if order_id in store.logged_ref_ids():
                return self._send(200, {"ok": True, "duplicate": True, "totals": nutrition})

            meal_type = str(body.get("meal_type") or "").strip()
            if not meal_type and order.created_at:
                from .coach import meal_of_time

                meal_type = meal_of_time(order.created_at)
            meal_type = meal_type or "lunch"

            targets = service.targets(meal=meal_type)
            store.log_meal(
                items=[
                    {
                        "name": str(i.get("name") or i.get("productName") or ""),
                        "qty": i.get("qty") or i.get("quantity") or 1,
                    }
                    for i in (order.items or [])
                ],
                kcal=nutrition["kcal"],
                protein=nutrition["protein"],
                fat=nutrition["fat"],
                carb=nutrition["carb"],
                sodium=nutrition["sodium"],
                target_kcal=targets.kcal,
                store=order.store_name,
                meal_type=meal_type,
                source="mcp-order",
                ref_id=order_id,
                ts=order.created_at,  # 用订单时间归档，时间线才对得上
                uid=uid,
            )
            self._log_wait(store, body.get("wait_min"), order_id, order.store_name)
            return self._send(
                200,
                {
                    "ok": True,
                    "totals": nutrition,
                    "meal_type": meal_type,
                    "store": order.store_name,
                    "commute_logged": commute_logged,
                    "kcal_burned": commute_logged.get("kcal", 0) if commute_logged else 0,
                },
            )

        items = body.get("items") or []
        if not items:
            # 只上报通勤（还没下单也想先记下这一趟）也是合法的
            if commute_logged:
                return self._send(200, {"ok": True, "commute_logged": commute_logged})
            return self._send(400, {"error": "items 不能为空"})
        totals = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carb": 0.0, "sodium": 0.0}
        for it in items:
            for k in totals:
                totals[k] += float(it.get(k, 0) or 0)
        target = body.get("target_kcal")
        store.log_meal(
            items=items,
            kcal=round(totals["kcal"], 1),
            protein=round(totals["protein"], 1),
            fat=round(totals["fat"], 1),
            carb=round(totals["carb"], 1),
            sodium=round(totals["sodium"], 1),
            target_kcal=float(target) if target else None,
            store=str(body.get("store") or ""),
            meal_type=str(body.get("meal_type") or "lunch"),
            source=str(body.get("source") or ""),
            uid=uid,
        )
        return self._send(200, {"ok": True, "totals": totals})


    def _post_commute(self):
        """单独记一次通勤消耗（还没下单，只想先记下走/骑过去的消耗）。"""
        body = self._body()
        logged = self._log_commute(
            _store(),
            body.get("commute") or body,
            str(body.get("ref_id") or "").strip(),
            uid=str(body.get("uid") or ""),
        )
        if not logged:
            return self._send(400, {"error": "缺少 distance_m / kcal"})
        return self._send(200, {"ok": True, "commute_logged": logged})

    @staticmethod
    def _log_commute(store: Any, payload: Any, ref_id: str, uid: str = "") -> Optional[Dict[str, Any]]:
        """记一次通勤消耗（步行/骑行的卡路里、里程、时长）。

        ref_id（订单号）用于去重：下单后每 2 分钟轮询一次，
        没有它同一趟会被重复累加。通勤可以独立记（不依赖订单）。
        uid 用于手机补传时的重传保护。
        """
        if not isinstance(payload, dict):
            return None
        try:
            kcal = float(payload.get("kcal") or 0)
            distance_m = float(payload.get("distance_m") or 0)
        except (TypeError, ValueError):
            return None
        if kcal <= 0 and distance_m <= 0:
            return None
        if uid and store.uid_rowid("commutes", uid):
            return {"duplicate": True, "kcal": 0}
        if ref_id and ref_id in store.logged_commute_refs():
            return {"duplicate": True, "kcal": 0}
        store.log_commute(
            store=str(payload.get("store") or ""),
            mode=str(payload.get("mode") or "walk"),
            distance_m=distance_m,
            minutes=float(payload.get("minutes") or 0),
            kcal=kcal,
            source=str(payload.get("source") or ("mcp-order" if ref_id else "web")),
            ref_id=ref_id,
            uid=uid,
        )
        return {"duplicate": False, "kcal": round(kcal, 1), "distance_m": round(distance_m, 1)}

    @staticmethod
    def _log_wait(store: Any, wait_min: Any, ref_id: str, store_name: str = "") -> Optional[int]:
        """把「等这一单的时间」记成一段成长时间。

        等麦当劳的时间本来是浪费掉的，这里把它变成可统计的专注时长
        （kind='wait'），和背单词（kind='words'）分开统计，但都算进 focus_minutes。
        """
        try:
            minutes = float(wait_min or 0)
        except (TypeError, ValueError):
            return None
        if minutes <= 0:
            return None
        with_meta = {"ref_id": ref_id}
        return store.log_session(
            kind="wait",
            duration_min=round(minutes, 1),
            meta=with_meta,
        )


def _find_order(service: Any, order_id: str):
    """在历史订单里按订单号找一单（order-detail 查不到时的兜底）。"""
    try:
        for order in service.order_history(limit=20):
            if order.order_id == order_id:
                return order
    except Exception:  # noqa: BLE001
        return None
    return None


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    # 启动即把词表种入数据库，保证 /api/words 有内容
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from .store import RecordStore

        store = RecordStore(Path(_DB_PATH))
        bank = ROOT / "data" / "wordbank.json"
        if bank.is_file():
            words = json.loads(bank.read_text(encoding="utf-8")).get("words", [])
            added = store.seed_wordbank(words)
            if added:
                print(f"  已种入 {added} 个单词到数据库")
    except Exception as exc:  # noqa: BLE001
        print(f"  词库预载跳过：{exc}")

    os.chdir(WEB_DIR)
    server = ThreadingHTTPServer((host, port), Handler)
    lan = _lan_url((host, port))
    print(f"麦麦成长时刻 · 本地服务已启动")
    print(f"  本机打开 http://127.0.0.1:{port}/")
    if lan:
        print(f"  手机连同一个 WiFi 后打开 {lan}")
        print(f"  （如果手机上打不开，用 python run.py web --host 0.0.0.0 重启，并放行防火墙）")
    print(f"  按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    run()
