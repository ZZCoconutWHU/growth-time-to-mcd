"""手机 ↔ 电脑同步：手机带出门断网也能背单词，回家连上 WiFi 自动补传。

场景是这样的：

  1. 在家里，电脑跑 `python run.py web --host 0.0.0.0`，手机连同一个 WiFi 访问
     `http://电脑IP:8000`。这时手机上的每一次背词、每一餐，都**直接写进电脑的
     records.db**，根本不需要同步——两台设备看的是同一个库。
  2. 但人是会出门的。手机一离开 WiFi 就访问不到电脑上的服务，
     于是页面进入「离线模式」：照常背单词、照常记一餐，
     写操作先排进浏览器的待发队列（localStorage），页面右上角标「待同步 N 条」。
  3. 回到家里（或任何能访问到电脑的网络）连上同一 WiFi，
     页面探测到服务可达，把队列里攒的记录按 uid 逐条补传。
     每条记录自带 uid，重复上传只会落一次，不会因为重试把卡路里算两遍。
  4. 万一两台设备物理上见不到面（比如手机没法连回家里的 WiFi），
     还有「导出 JSON → 传文件 → 导入」这条兜底：导出的就是同一份快照格式。

设计原则：
  - **幂等优先**：所有写入都带 uid，服务端按 uid 去重；
  - **只增不删**：同步只做合并，绝不删对方的数据，冲突时取「信息量更大」的一侧
    （单词进度取 max、档案取更新的 updated_at）；
  - **不联网也能用**：同步失败不影响本地继续记录。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .store import RecordStore

# 一条待同步记录的形状：
#   {"type": "play|meal|commute|wait|profile|anchor", "uid": "...", "ts": "ISO", "payload": {...}}
# uid 由产生它的设备生成（设备号 + 时间戳 + 随机），保证跨设备唯一。
OP_TYPES = ("play", "meal", "commute", "wait", "profile", "anchor")


def new_uid(device: str = "") -> str:
    """生成一条记录的唯一编号（设备号 + 时间 + 随机，够用且不依赖 uuid 库）。"""
    import random
    import time

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    salt = "%06x" % random.randint(0, 0xFFFFFF)
    return f"{device or 'dev'}-{stamp}-{salt}"


def _parse_ts(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip().replace("T", " ")
    if not text:
        return None
    for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:size], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _num(payload: Dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(payload.get(key) or default)
    except (TypeError, ValueError):
        return default


def apply_ops(
    store: RecordStore,
    ops: List[Dict[str, Any]],
    device: str = "",
    resolve_order: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """把一批离线记录并入本机档案。

    resolve_order 是「按订单号补出真实营养」的回调（要连 MCP，只有在线时才有）：
    手机离线时排队的通常是手填记录，用不到它。
    """
    applied: List[str] = []
    skipped: List[str] = []
    errors: List[Dict[str, Any]] = []

    for op in ops or []:
        if not isinstance(op, dict):
            continue
        op_type = str(op.get("type") or "").strip()
        payload = op.get("payload") if isinstance(op.get("payload"), dict) else {}
        uid = str(op.get("uid") or "")
        ts = _parse_ts(op.get("ts") or payload.get("ts"))
        try:
            if op_type == "play":
                ok = apply_play(store, payload, uid, ts)
            elif op_type == "meal":
                ok = apply_meal(store, payload, uid, ts, resolve_order)
            elif op_type == "commute":
                ok = apply_commute(store, payload, uid, ts)
            elif op_type == "wait":
                ok = apply_wait(store, payload, uid, ts)
            elif op_type == "profile":
                apply_profile(store, payload)
                ok = True
            elif op_type == "anchor":
                apply_anchor(store, payload)
                ok = True
            else:
                errors.append({"uid": uid, "type": op_type, "error": f"未知的记录类型 {op_type}"})
                continue
            (applied if ok else skipped).append(uid or op_type)
        except Exception as exc:  # noqa: BLE001 一条失败不能拖垮整批
            errors.append({"uid": uid, "type": op_type, "error": str(exc)})

    if device:
        store.set_meta("last_device", device)
    store.set_meta("last_sync_at", datetime.now().isoformat(timespec="seconds"))
    return {
        "applied": len(applied),
        "skipped": len(skipped),
        "errors": errors,
        "applied_uids": applied,
        "skipped_uids": skipped,
    }


def apply_play(store: RecordStore, payload: Dict[str, Any], uid: str, ts: Optional[datetime]) -> bool:
    """补传一局背单词：先按 uid 去重，再写单词进度 + 专注时长。"""
    if uid and store._uid_rowid("sessions", uid):
        return False
    words = payload.get("words") or []
    if not words:
        return False
    correct = 0
    for item in words:
        en = str((item or {}).get("en") or "").strip()
        if not en:
            continue
        ok = bool((item or {}).get("correct"))
        store.record_word(en, ok)
        if ok:
            correct += 1
    store.log_session(
        kind="words",
        duration_min=_num(payload, "duration_min"),
        words=len(words),
        correct=correct,
        ts=ts,
        uid=uid,
    )
    return True


def apply_meal(
    store: RecordStore,
    payload: Dict[str, Any],
    uid: str,
    ts: Optional[datetime],
    resolve_order: Optional[Callable[[str], Optional[Dict[str, Any]]]],
) -> bool:
    """补传一餐。

    两种形态：
      - 带 order_id：交给 resolve_order 去 MCP 拉真实明细与营养（只在在线时可能）；
      - 手填 items：手机上自己记的一餐，直接落库。
    """
    if uid and store._uid_rowid("meals", uid):
        return False
    order_id = str(payload.get("order_id") or "").strip()
    if order_id:
        if not callable(resolve_order):
            return False
        detail = resolve_order(order_id)
        if not detail:
            return False
        if order_id in store.logged_ref_ids():
            return False
        store.log_meal(
            items=detail.get("items") or [],
            kcal=_num(detail, "kcal"),
            protein=_num(detail, "protein"),
            fat=_num(detail, "fat"),
            carb=_num(detail, "carb"),
            sodium=_num(detail, "sodium"),
            target_kcal=detail.get("target_kcal"),
            store=str(detail.get("store") or ""),
            meal_type=str(detail.get("meal_type") or "lunch"),
            source="mcp-order",
            ref_id=order_id,
            ts=_parse_ts(detail.get("ts")) or ts,
            uid=uid,
        )
        return True

    items = payload.get("items") or []
    if not items:
        return False
    totals = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carb": 0.0, "sodium": 0.0}
    for it in items:
        for k in totals:
            totals[k] += _num(it, k)
    target = payload.get("target_kcal")
    store.log_meal(
        items=items,
        kcal=round(totals["kcal"], 1),
        protein=round(totals["protein"], 1),
        fat=round(totals["fat"], 1),
        carb=round(totals["carb"], 1),
        sodium=round(totals["sodium"], 1),
        target_kcal=float(target) if target else None,
        store=str(payload.get("store") or ""),
        meal_type=str(payload.get("meal_type") or "lunch"),
        source=str(payload.get("source") or "phone"),
        ref_id=str(payload.get("ref_id") or ""),
        ts=ts,
        uid=uid,
    )
    return True


def apply_commute(store: RecordStore, payload: Dict[str, Any], uid: str, ts: Optional[datetime]) -> bool:
    """补传一趟通勤消耗（手机在路上记的）。"""
    if uid and store._uid_rowid("commutes", uid):
        return False
    kcal = _num(payload, "kcal")
    distance_m = _num(payload, "distance_m")
    if kcal <= 0 and distance_m <= 0:
        return False
    ref_id = str(payload.get("ref_id") or "")
    if ref_id and ref_id in store.logged_commute_refs():
        return False
    store.log_commute(
        store=str(payload.get("store") or ""),
        mode=str(payload.get("mode") or "walk"),
        distance_m=distance_m,
        minutes=_num(payload, "minutes"),
        kcal=kcal,
        source=str(payload.get("source") or "phone"),
        ref_id=ref_id,
        ts=ts,
        uid=uid,
    )
    return True


def apply_wait(store: RecordStore, payload: Dict[str, Any], uid: str, ts: Optional[datetime]) -> bool:
    """补传一段等餐时间（kind='wait' 的专注时长）。"""
    if uid and store._uid_rowid("sessions", uid):
        return False
    minutes = _num(payload, "minutes") or _num(payload, "duration_min")
    if minutes <= 0:
        return False
    store.log_session(
        kind="wait",
        duration_min=round(minutes, 1),
        meta={"ref_id": str(payload.get("ref_id") or "")},
        ts=ts,
        uid=uid,
    )
    return True


def apply_profile(store: RecordStore, payload: Dict[str, Any]) -> None:
    from .store import Profile

    if not payload.get("height_cm") or not payload.get("weight_kg"):
        return
    store.save_profile(
        Profile(
            height_cm=_num(payload, "height_cm"),
            weight_kg=_num(payload, "weight_kg"),
            age=int(_num(payload, "age")),
            sex=str(payload.get("sex") or "male"),
            activity=str(payload.get("activity") or "light"),
            goal=str(payload.get("goal") or "maintain"),
        )
    )


def apply_anchor(store: RecordStore, payload: Dict[str, Any]) -> None:
    store.upsert_food_anchor(
        name=str(payload.get("name") or ""),
        category=str(payload.get("category") or ""),
        price=_num(payload, "price"),
        kcal=_num(payload, "kcal"),
        protein=_num(payload, "protein"),
        fat=_num(payload, "fat"),
        carb=_num(payload, "carb"),
        sodium=_num(payload, "sodium"),
        source=str(payload.get("source") or "sync"),
    )
