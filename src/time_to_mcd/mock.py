"""离线演示模式。

评委 / 面试官不一定会马上去申请 MCP Token，所以提供一套与真实返回结构
一致的 fixtures：`--mock` 下整条链路（门店 → 营养 → 候餐 → 报告）都能跑通，
接上 Token 后切换到真实 MCP，业务代码零改动。

fixtures 只是「演示数据」，README 与报告里都会标注来源，不冒充实时结果。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schema import _ALIASES, _norm  # 复用同一套别名规则

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures"

_FIXTURE_MAP: Dict[str, str] = {
    "query-nearby-stores": "nearby_stores.json",
    "delivery-query-stores": "nearby_stores.json",
    "delivery-query-addresses": "delivery_addresses.json",
    "list-nutrition-foods": "nutrition_foods.json",
    "query-meals": "menu.json",
    "query-meal-detail": "menu.json",
    "query-order": "order_detail.json",
    "order-list": "order_list.json",
    "now-time-info": "now_time.json",
}

_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "query-nearby-stores",
        "description": "查询用户提供地址附近的麦当劳餐厅",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "cityName": {"type": "string"},
                "pageSize": {"type": "integer"},
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "delivery-query-stores",
        "description": "外送场景下查询用户收货地址附近可配送门店",
        "inputSchema": {
            "type": "object",
            "properties": {"address": {"type": "string"}, "keyword": {"type": "string"}},
            "required": ["address"],
        },
    },
    {
        "name": "delivery-query-addresses",
        "description": "查询用户已创建的配送地址列表",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list-nutrition-foods",
        "description": "获取麦当劳常见餐品的营养成分数据",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "query-meals",
        "description": "查询当前门店可售卖的餐品菜单",
        "inputSchema": {
            "type": "object",
            "properties": {
                "storeCode": {"type": "string"},
                "keyword": {"type": "string"},
            },
            "required": ["storeCode"],
        },
    },
    {
        "name": "query-meal-detail",
        "description": "根据餐品编码查询餐品详情套餐组成",
        "inputSchema": {
            "type": "object",
            "properties": {"storeCode": {"type": "string"}, "mealCode": {"type": "string"}},
            "required": ["storeCode", "mealCode"],
        },
    },
    {
        "name": "query-order",
        "description": "查询订单状态、订单内容、配送信息",
        "inputSchema": {
            "type": "object",
            "properties": {"orderId": {"type": "string"}},
            "required": ["orderId"],
        },
    },
    {
        "name": "order-list",
        "description": "查询近期的到店/外送历史订单",
        "inputSchema": {"type": "object", "properties": {"pageSize": {"type": "integer"}}},
    },
    {
        "name": "now-time-info",
        "description": "返回当前的完整时间信息",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

# 官方 Server 的其余工具：本项目不调用，但保留在清单里，
# 让 `--mock tools` 与真实服务端的能力面对齐（字段未声明即为空 schema）。
_EXTRA_TOOLS: List[Dict[str, Any]] = [
    {"name": "delivery-create-address", "description": "新增配送地址"},
    {"name": "query-meal-assistance", "description": "仅企业团餐场景下，查询该门店支持的助餐服务"},
    {"name": "query-store-coupons", "description": "查询用户在当前门店下可使用的优惠券列表"},
    {"name": "calculate-price", "description": "根据商品列表（可含优惠券）计算金额、配送费与应付总价"},
    {"name": "create-order", "description": "根据门店、就餐方式、商品列表创建订单，返回支付链接"},
    {"name": "cancel-order", "description": "取消点餐订单"},
    {"name": "campaign-calendar", "description": "查询麦当劳中国当月营销活动日历"},
    {"name": "available-coupons", "description": "查询用户当前可领取的麦麦省优惠券列表"},
    {"name": "auto-bind-coupons", "description": "一键领取麦麦省所有当前可用优惠券"},
    {"name": "query-my-coupons", "description": "查询账户下所有可用优惠券"},
    {"name": "query-my-account", "description": "查询积分账户信息（可用、累计、冻结、即将过期）"},
    {"name": "mall-points-products", "description": "查询麦麦商城可使用积分兑换或现金购买的商品"},
    {"name": "mall-product-detail", "description": "查询麦麦商城商品详情"},
    {"name": "mall-create-order", "description": "使用积分兑换虚拟或实物商品"},
    {"name": "query-lottery-info", "description": "查询积分抽奖活动状态、奖品与消耗规则"},
    {"name": "draw-lottery", "description": "执行一次积分抽奖"},
    {"name": "query-my-prizes", "description": "查询积分抽奖获得的所有奖品"},
    {"name": "mall-order-list", "description": "查询麦麦商城近一年商品订单"},
    {"name": "mall-order-detail", "description": "查询麦麦商城订单详情"},
    {"name": "query-party-city", "description": "查询主题活动商品对应的可参与城市"},
    {"name": "query-party-store", "description": "查询指定城市下主题活动可参与门店"},
    {"name": "query-partystore-date", "description": "查询指定门店主题活动可预约日期"},
    {"name": "query-partystore-session", "description": "查询指定门店、日期下可预约场次"},
    {"name": "party-order-create", "description": "创建主题活动订单"},
]

_TOOL_SCHEMAS.extend(
    {**tool, "inputSchema": tool.get("inputSchema", {"type": "object", "properties": {}})}
    for tool in _EXTRA_TOOLS
)

_TIME_TOKENS = ("createTime", "promiseTime", "orderTime", "estimatedTime")


def _expand_times(node: Any) -> Any:
    """把 fixtures 里的 {{now-6m}} / {{now+7m}} 展开成真实时间。"""
    now = datetime.now()
    if isinstance(node, dict):
        return {key: _expand_times(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_expand_times(item) for item in node]
    if isinstance(node, str) and node.startswith("{{now") and node.endswith("}}"):
        expr = node[5:-2].strip()
        sign = -1 if expr.startswith("-") else 1
        digits = "".join(ch for ch in expr if ch.isdigit())
        minutes = int(digits or 0) * sign
        return (now + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    return node


class MockBox:
    """与 ToolBox 接口一致的假工具箱。"""

    def __init__(self, fixture_dir: Optional[Path] = None):
        self.fixture_dir = fixture_dir or FIXTURE_DIR
        self._tools = {tool["name"]: tool for tool in _TOOL_SCHEMAS}
        self.calls: List[str] = []

    # ---- 与 ToolBox 对齐的只读接口
    @property
    def tools(self) -> Dict[str, Dict[str, Any]]:
        return self._tools

    def has(self, name: str) -> bool:
        return name in self._tools

    def require(self, *names: str) -> str:
        for name in names:
            if self.has(name):
                return name
        raise RuntimeError(f"mock 未提供工具 {names}")

    def properties(self, name: str) -> Dict[str, Any]:
        return self._tools[name]["inputSchema"].get("properties", {})

    def required(self, name: str) -> List[str]:
        return list(self._tools[name]["inputSchema"].get("required") or [])

    def prepare(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """真实模式下会做字段映射；mock 只校验必填项有没有给到。"""
        props = {_norm(key): key for key in self.properties(name)}
        prepared: Dict[str, Any] = {}
        leftovers = {}
        for key, value in arguments.items():
            target = props.get(_norm(key))
            if target:
                prepared[target] = value
            else:
                leftovers[key] = value
        for needed in self.required(name):
            if needed in prepared:
                continue
            for alias in [needed, *_ALIASES.get(_norm(needed), [])]:
                actual = props.get(_norm(alias))
                if actual and actual in leftovers:
                    prepared[needed] = leftovers.pop(actual)
                    break
        prepared.update(leftovers)
        return prepared

    # ---- 调用
    def call(self, name: str, **arguments: Any) -> Any:
        self.calls.append(name)
        if name == "now-time-info":
            return {"timestamp": int(time.time()), "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        fixture = _FIXTURE_MAP.get(name)
        if not fixture:
            raise RuntimeError(f"mock 未覆盖工具 {name}")
        path = self.fixture_dir / fixture
        if not path.is_file():
            raise RuntimeError(f"缺少 fixture：{path}")
        return _expand_times(json.loads(path.read_text(encoding="utf-8")))

    def call_raw(self, name: str, **arguments: Any) -> Dict[str, Any]:
        return {"content": [{"type": "text", "text": json.dumps(self.call(name, **arguments), ensure_ascii=False)}]}

    def describe(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": name,
                "description": tool.get("description", ""),
                "required": self.required(name),
                "properties": list(self.properties(name)),
            }
            for name, tool in sorted(self._tools.items())
        ]
