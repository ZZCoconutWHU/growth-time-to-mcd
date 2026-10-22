"""候餐模块：订单状态归一化、剩余等待时间估算、出发时刻建议。

设计要点：
  - 麦当劳订单状态字段在不同场景（麦乐送 / 到店取餐 / 得来速）下叫法不同，
    这里统一映射到 6 个阶段，后续估算只依赖阶段，不依赖原始文案。
  - 有「预计完成时间」时以服务端承诺时间为准；缺失时才用阶段经验值兜底，
    并在输出里标注置信度，不假装精确。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

STAGES: List[str] = [
    "待支付",
    "已支付·待接单",
    "制作中",
    "待取餐",
    "配送中",
    "已完成",
    "已取消",
]

# 阶段索引
S_PENDING_PAY, S_PAID, S_COOKING, S_READY, S_DELIVERING, S_DONE, S_CANCELLED = range(7)

_STAGE_HINTS: Dict[int, Sequence[str]] = {
    S_CANCELLED: ("取消", "cancel", "refund", "退款", "关闭", "closed"),
    S_DONE: ("已完成", "完成", "已取餐", "已送达", "已收货", "completed", "finished", "done", "picked"),
    S_DELIVERING: ("配送中", "骑手", "取货中", "在路上", "delivering", "shipping", "rider"),
    S_READY: ("待取餐", "已出餐", "出餐", "可取餐", "待取", "ready", "prepared", "待自取"),
    S_COOKING: ("制作中", "备餐", "制作", "已接单", "处理中", "cooking", "preparing", "accepted", "making"),
    S_PAID: ("已支付", "待接单", "待制作", "已下单", "paid", "pending", "placed"),
    S_PENDING_PAY: ("待支付", "未支付", "待付款", "unpaid", "waiting_pay"),
}

MODES = {
    "dinein": "堂食",
    "takeaway": "外带/到店取餐",
    "delivery": "麦乐送",
    "drivethru": "得来速",
}

# 各阶段经验剩余时间（分钟），仅在没有承诺时间时兜底
_DEFAULT_REMAINING = {
    S_PAID: {"takeaway": 12, "dinein": 12, "drivethru": 10, "delivery": 25},
    S_COOKING: {"takeaway": 7, "dinein": 7, "drivethru": 5, "delivery": 18},
    S_READY: {"takeaway": 0, "dinein": 0, "drivethru": 0, "delivery": 12},
    S_DELIVERING: {"takeaway": 0, "dinein": 0, "drivethru": 0, "delivery": 12},
}

_KEY_HINTS = {
    "order_id": ["orderId", "orderNo", "orderSn", "orderCode", "id"],
    # orderStatus 必须排在 status 前：实测 status 是状态码（"40"），
    # orderStatus 才是人话（"订单已完成"），取错就显示成一串数字。
    "status": ["orderStatus", "statusText", "orderStatusName", "state", "status"],
    "status_code": ["statusCode", "orderStatusCode", "code"],
    "created": ["createTime", "createdAt", "orderTime", "orderDate", "createDate"],
    "promise": ["promiseTime", "expectTime", "estimatedTime", "deliveryTime", "finishTime", "预计送达"],
    "store": ["storeName", "store", "beName", "restaurantName"],
    "mode": ["mode", "orderType", "takeWay", "takeawayType", "deliveryType", "channel"],
    "amount": ["realTotalAmount", "totalAmount", "payAmount", "amount", "actualAmount"],
    "pickup": ["pickupCode", "takeMealCode", "pickupNo", "authCode", "取餐码"],
}


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


def parse_time(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e11:  # 毫秒
            number /= 1000
        try:
            return datetime.fromtimestamp(number)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit():
        return parse_time(int(text))
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            return datetime.strptime(text.replace("+08:00", ""), fmt)
        except ValueError:
            continue
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 13:
        return parse_time(int(digits[:13]))
    return None


def detect_stage(status_text: str, status_code: Any = None) -> int:
    text = f"{status_text or ''} {status_code if status_code is not None else ''}".lower()
    for stage, hints in _STAGE_HINTS.items():
        if any(hint in text for hint in hints):
            return stage
    return S_PAID


def detect_mode(value: Any) -> str:
    text = str(value or "").lower()
    if any(k in text for k in ("deliver", "外送", "麦乐送", "外卖")):
        return "delivery"
    if any(k in text for k in ("drive", "得来速", "车道")):
        return "drivethru"
    if any(k in text for k in ("dine", "堂食", "eat_in", "instore")):
        return "dinein"
    return "takeaway"


@dataclass
class Order:
    order_id: str
    stage: int = S_PAID
    status_text: str = ""
    created_at: Optional[datetime] = None
    promise_at: Optional[datetime] = None
    store_name: str = ""
    mode: str = "takeaway"
    amount: str = ""
    pickup_code: str = ""
    items: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def stage_label(self) -> str:
        return STAGES[self.stage] if 0 <= self.stage < len(STAGES) else "未知"

    @property
    def mode_label(self) -> str:
        return MODES.get(self.mode, self.mode)

    @property
    def is_final(self) -> bool:
        return self.stage in (S_DONE, S_CANCELLED)


@dataclass
class ETA:
    remaining_min: Optional[float]
    confidence: str  # high / medium / low
    basis: str
    ready_at: Optional[datetime] = None


@dataclass
class DeparturePlan:
    leave_at: Optional[datetime]
    arrive_at: Optional[datetime]
    ready_at: Optional[datetime]
    advice: str
    tone: str  # go / wait / hurry / done / blocked
    notes: List[str] = field(default_factory=list)


def _extract_items(node: Any, depth: int = 0) -> List[Dict[str, Any]]:
    if depth > 4:
        return []
    items: List[Dict[str, Any]] = []
    if isinstance(node, list):
        for entry in node:
            if isinstance(entry, dict):
                name = _pick(entry, ["name", "productName", "mealName", "itemName"])
                if name:
                    qty = _pick(entry, ["qty", "quantity", "count", "num"]) or 1
                    item = {"name": str(name), "qty": float(qty) if str(qty).replace(".", "").isdigit() else 1}
                    # 单价：营养库里查不到的商品要靠它按「同类价格最近」估算
                    price = _pick(entry, ["price", "unitPrice", "salePrice", "productPrice", "amount"])
                    price_num = _num(price)
                    if price_num > 0:
                        item["price"] = price_num
                    combo = _extract_combo(entry)
                    if combo:
                        item["combo"] = combo
                    items.append(item)
            elif isinstance(entry, str):
                items.append({"name": entry, "qty": 1})
    elif isinstance(node, dict):
        # 实测 query-order 的明细字段名叫 orderProductList（不是 productList），
        # 漏了它就永远解析不出商品，历史订单只能显示空。
        for key in (
            "items", "products", "meals", "orderItems", "productList",
            "orderProductList", "itemList", "details",
        ):
            if key in node:
                items.extend(_extract_items(node[key], depth + 1))
    return items


def _extract_combo(entry: Dict[str, Any], depth: int = 0) -> List[Dict[str, Any]]:
    """套餐里的子项（comboItemList），套餐名匹配不到营养库时用它兜底。"""
    if depth > 2:
        return []
    out: List[Dict[str, Any]] = []
    raw = entry.get("comboItemList") or entry.get("comboItems") or []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    for child in raw:
        if not isinstance(child, dict):
            continue
        name = _pick(child, ["name", "productName", "mealName", "itemName"])
        if not name:
            continue
        qty = _pick(child, ["qty", "quantity", "count", "num"]) or 1
        child_item = {"name": str(name), "qty": float(qty) if str(qty).replace(".", "").isdigit() else 1}
        price = _num(_pick(child, ["price", "unitPrice", "salePrice", "productPrice", "amount"]))
        if price > 0:
            child_item["price"] = price
        out.append(child_item)
    return out


def _num(value: Any) -> float:
    """从各种写法（"5" / "5.00元" / 5）里取出数字。"""
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group()) if match else 0.0


def normalize_order(raw: Any) -> Optional[Order]:
    """把任意订单结构（订单详情 / 订单列表项）转成 Order。"""
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not isinstance(raw, dict):
        return None

    # 常见外层包装
    for key in ("data", "order", "orderInfo", "result"):
        if isinstance(raw.get(key), dict):
            raw = raw[key]
            break

    status_text = str(_pick(raw, _KEY_HINTS["status"]) or "")
    status_code = _pick(raw, _KEY_HINTS["status_code"])
    order_id = str(_pick(raw, _KEY_HINTS["order_id"]) or "")

    if not status_text and not order_id:
        return None

    return Order(
        order_id=order_id,
        stage=detect_stage(status_text, status_code),
        status_text=status_text or "未知状态",
        created_at=parse_time(_pick(raw, _KEY_HINTS["created"])),
        promise_at=parse_time(_pick(raw, _KEY_HINTS["promise"])),
        store_name=str(_pick(raw, _KEY_HINTS["store"]) or ""),
        mode=detect_mode(_pick(raw, _KEY_HINTS["mode"])),
        amount=str(_pick(raw, _KEY_HINTS["amount"]) or ""),
        pickup_code=str(_pick(raw, _KEY_HINTS["pickup"]) or ""),
        items=_extract_items(raw),
        raw=raw,
    )


def extract_orders(payload: Any) -> List[Order]:
    orders: List[Order] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for entry in node:
                walk(entry)
        elif isinstance(node, dict):
            if any(k in node for k in ("orderId", "orderNo", "orderSn")):
                order = normalize_order(node)
                if order:
                    orders.append(order)
                return
            for value in node.values():
                walk(value)

    walk(payload)
    seen = set()
    unique = []
    for order in orders:
        key = order.order_id or id(order)
        if key in seen:
            continue
        seen.add(key)
        unique.append(order)
    return unique


def estimate_eta(order: Order, now: Optional[datetime] = None) -> ETA:
    now = now or datetime.now()

    if order.stage == S_CANCELLED:
        return ETA(None, "high", "订单已取消，无需等待")
    if order.stage == S_DONE:
        return ETA(0.0, "high", "订单已完成", now)

    if order.promise_at:
        remaining = (order.promise_at - now).total_seconds() / 60
        confidence = "high"
        basis = f"服务端承诺时间 {order.promise_at.strftime('%H:%M')}"
        if remaining < 0:
            return ETA(0.0, "high", f"{basis}，已到预计时间，可能随时可取", order.promise_at)
        return ETA(round(remaining, 1), confidence, basis, order.promise_at)

    if order.stage == S_PENDING_PAY:
        return ETA(None, "high", "订单待支付，支付后才会进入备餐")

    table = _DEFAULT_REMAINING.get(order.stage, {})
    minutes = table.get(order.mode, table.get("takeaway"))
    if minutes is None:
        return ETA(None, "low", "状态无法识别，建议以门店实际出餐为准")

    # 若已知下单时间，用「已过去多久」做一次校准，避免状态更新滞后导致高估
    if order.created_at:
        elapsed = (now - order.created_at).total_seconds() / 60
        typical_total = {"takeaway": 14, "dinein": 14, "drivethru": 11, "delivery": 32}.get(order.mode, 14)
        by_time = typical_total - elapsed
        minutes = max(0.0, min(minutes + 2, max(by_time, minutes - 2)))

    return ETA(round(minutes, 1), "medium", f"基于阶段「{order.stage_label}」的经验值", now + timedelta(minutes=minutes))


def plan_departure(
    eta: ETA,
    travel_min: float = 10.0,
    now: Optional[datetime] = None,
    buffer_min: float = 2.0,
) -> DeparturePlan:
    """结合「还要等多久」与「走过去要多久」给出出发建议。"""
    now = now or datetime.now()
    notes: List[str] = []

    if eta.remaining_min is None:
        return DeparturePlan(None, None, None, f"暂时无法估算等待时间（{eta.basis}）。", "blocked", notes)

    if eta.remaining_min <= 0 and eta.confidence == "high" and eta.basis.startswith("订单已完成"):
        return DeparturePlan(None, None, eta.ready_at, "订单已完成，无需再等。", "done", notes)

    ready_at = eta.ready_at or (now + timedelta(minutes=eta.remaining_min))
    leave_at = ready_at - timedelta(minutes=travel_min + buffer_min)
    arrive_at = leave_at + timedelta(minutes=travel_min)

    if leave_at <= now:
        late = (now - leave_at).total_seconds() / 60
        if late <= 3:
            tone, advice = "go", "现在正是出发的时候，走到店餐刚好出炉。"
        elif eta.remaining_min > 1:
            tone, advice = "hurry", f"按当前路程建议已提前 {late:.0f} 分钟出发，现在走仍能在出餐后不久到店。"
        else:
            tone, advice = "go", "餐已就绪，现在出发即可直接取餐。"
        leave_at = now
    else:
        wait_min = (leave_at - now).total_seconds() / 60
        tone = "wait"
        advice = f"还有 {wait_min:.0f} 分钟再动身最合适，早到只会站着等。"

    if eta.confidence != "high":
        notes.append("等待时间为经验估算，建议在出发前 1–2 分钟再看一次订单状态。")
    if eta.remaining_min > travel_min + 10:
        notes.append("等待时间较长，可以先做别的事，本页会在建议出发时刻再提醒你。")

    return DeparturePlan(leave_at, arrive_at, ready_at, advice, tone, notes)


def timeline(plan: DeparturePlan, now: Optional[datetime] = None) -> List[str]:
    now = now or datetime.now()
    rows: List[str] = []
    if plan.leave_at:
        rows.append(f"{plan.leave_at.strftime('%H:%M')} 出发")
    if plan.arrive_at:
        rows.append(f"{plan.arrive_at.strftime('%H:%M')} 到店")
    if plan.ready_at:
        rows.append(f"{plan.ready_at.strftime('%H:%M')} 预计出餐")
    return rows


def progress_ratio(order: Order, now: Optional[datetime] = None) -> float:
    """粗略进度条：有承诺时间用时间比，否则用阶段比。"""
    stage_ratio = {
        S_PENDING_PAY: 0.05,
        S_PAID: 0.2,
        S_COOKING: 0.55,
        S_READY: 0.9,
        S_DELIVERING: 0.75,
        S_DONE: 1.0,
        S_CANCELLED: 0.0,
    }
    if order.promise_at and order.created_at and order.promise_at > order.created_at:
        now = now or datetime.now()
        span = (order.promise_at - order.created_at).total_seconds()
        ratio = (now - order.created_at).total_seconds() / span
        return max(0.02, min(0.99, ratio))
    return stage_ratio.get(order.stage, 0.3)
