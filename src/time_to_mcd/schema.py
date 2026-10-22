"""按工具真实的 inputSchema 组装参数。

麦当劳 MCP 的参数命名（如 storeCode / beCode / storeId）可能随版本微调，
因此本项目不硬编码字段名，而是在 tools/list 拿到 inputSchema 后做一次
「归一化 + 别名映射」，避免版本升级后整个 Skill 直接失效。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from .mcp_client import MCPClient, extract_payload


class SchemaError(RuntimeError):
    pass


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


# 语义别名：左为语义名，右为可能的实际字段名
_ALIASES: Dict[str, List[str]] = {
    "storecode": ["store_code", "storeid", "store_id", "becode", "be_code", "restcode", "restaurantcode"],
    "storeid": ["store_id", "storecode", "store_code", "becode"],
    "keyword": ["query", "q", "keywords", "search", "address", "addr"],
    "address": ["addr", "keyword", "query", "location"],
    "citycode": ["city_code", "city", "cityid"],
    "cityname": ["city_name", "city"],
    "mealcode": ["meal_code", "productcode", "product_code", "itemcode", "item_code", "sku", "skuCode"],
    "productcode": ["product_code", "mealcode", "meal_code", "itemcode", "skucode"],
    "orderid": ["order_id", "orderNo", "order_no"],
    "couponid": ["coupon_id", "couponCode", "coupon_code"],
    "items": ["itemlist", "item_list", "products", "meallist", "cart"],
    "meals": ["items", "itemlist", "products"],
    "date": ["day", "reservedate", "reserve_date"],
}


class ToolBox:
    """带缓存的工具调用器。"""

    def __init__(self, client: MCPClient):
        self.client = client
        self._tools: Optional[Dict[str, Dict[str, Any]]] = None

    # ------------------------------------------------------------------ 工具表
    @property
    def tools(self) -> Dict[str, Dict[str, Any]]:
        if self._tools is None:
            self._tools = {tool["name"]: tool for tool in self.client.list_tools()}
        return self._tools

    def has(self, name: str) -> bool:
        return name in self.tools

    def require(self, *names: str) -> str:
        """返回第一个存在的工具名，全部缺失则报错。"""
        for name in names:
            if self.has(name):
                return name
        raise SchemaError(
            f"当前 MCP Server 未提供工具 {names}。可用工具：{', '.join(sorted(self.tools))}"
        )

    def properties(self, name: str) -> Dict[str, Any]:
        return (self.tools[name].get("inputSchema") or {}).get("properties") or {}

    def required(self, name: str) -> List[str]:
        return list((self.tools[name].get("inputSchema") or {}).get("required") or [])

    # ------------------------------------------------------------------ 参数装配
    def prepare(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name not in self.tools:
            raise SchemaError(f"未知工具：{name}")

        props = self.properties(name)
        if not props:
            return dict(arguments)

        by_norm = {_norm(prop): prop for prop in props}
        prepared: Dict[str, Any] = {}
        leftovers: Dict[str, Any] = {}

        for key, value in arguments.items():
            if value is None:
                continue
            target = by_norm.get(_norm(key))
            if target:
                prepared[target] = value
            else:
                leftovers[key] = value

        for needed in self.required(name):
            if needed in prepared:
                continue
            candidates: Iterable[str] = [needed, *_ALIASES.get(_norm(needed), [])]
            for candidate in candidates:
                actual = by_norm.get(_norm(candidate))
                if actual and actual in prepared:
                    prepared[needed] = prepared.pop(actual)
                    break
            if needed in prepared:
                continue
            for extra_key in list(leftovers):
                if _norm(extra_key) == _norm(needed):
                    prepared[needed] = leftovers.pop(extra_key)
                    break

        # 仍缺必填项时，用语义别名再兜一轮
        missing = [n for n in self.required(name) if n not in prepared]
        if missing:
            for needed in list(missing):
                for alias in _ALIASES.get(_norm(needed), []):
                    actual = by_norm.get(_norm(alias))
                    if actual and actual in leftovers:
                        prepared[needed] = leftovers.pop(actual)
                        missing.remove(needed)
                        break

        if missing:
            raise SchemaError(
                f"调用 {name} 缺少必填参数 {missing}。该工具可用字段：{list(props)}。"
                f"请用 --arg key=value 显式传入。"
            )

        prepared.update(leftovers)
        return prepared

    # ------------------------------------------------------------------ 调用
    def call(self, name: str, **arguments: Any) -> Any:
        payload = self.prepare(name, arguments)
        return extract_payload(self.client.call_tool(name, payload))

    def call_raw(self, name: str, **arguments: Any) -> Dict[str, Any]:
        payload = self.prepare(name, arguments)
        return self.client.call_tool(name, payload)

    def describe(self) -> List[Dict[str, Any]]:
        rows = []
        for name, tool in sorted(self.tools.items()):
            rows.append(
                {
                    "name": name,
                    "description": (tool.get("description") or "").strip(),
                    "required": self.required(name),
                    "properties": list(self.properties(name)),
                }
            )
        return rows
