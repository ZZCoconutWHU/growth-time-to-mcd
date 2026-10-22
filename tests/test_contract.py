"""工具契约测试：确保代码里用到的工具名与麦当劳 MCP 官方清单一致。

官方工具清单来源：M-China/mcd-mcp-server README「工具列表」（2026-09-10 / v1.0.9，共 33 个）。
一旦官方改名或本项目误写工具名，这里会立刻失败。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from time_to_mcd.mock import MockBox  # noqa: E402
from time_to_mcd.service import USED_TOOLS  # noqa: E402

# 官方 README 工具列表（顺序与文档一致）
OFFICIAL_TOOLS = {
    # 4.2 点餐
    "list-nutrition-foods",
    "delivery-query-addresses",
    "delivery-create-address",
    "delivery-query-stores",
    "query-meal-assistance",
    "query-nearby-stores",
    "query-store-coupons",
    "query-meals",
    "query-meal-detail",
    "calculate-price",
    "create-order",
    "cancel-order",
    "query-order",
    "order-list",
    # 4.3 互动与内容
    "campaign-calendar",
    "query-lottery-info",
    "draw-lottery",
    "query-my-prizes",
    # 4.4 麦麦省领券
    "available-coupons",
    "auto-bind-coupons",
    "query-my-coupons",
    # 其他
    "query-my-account",
    "mall-points-products",
    "mall-product-detail",
    "mall-create-order",
    "mall-order-list",
    "mall-order-detail",
    "now-time-info",
    "query-party-city",
    "query-party-store",
    "query-partystore-date",
    "query-partystore-session",
    "party-order-create",
}

# 本项目明确不调用、且理由必须写进 MCP_INTEGRATION.md 的工具
WRITE_TOOLS = {
    "create-order",
    "cancel-order",
    "mall-create-order",
    "party-order-create",
    "auto-bind-coupons",
    "delivery-create-address",
    "draw-lottery",
}


class ToolContractTest(unittest.TestCase):
    def test_used_tools_all_exist_officially(self):
        unknown = set(USED_TOOLS) - OFFICIAL_TOOLS
        self.assertEqual(unknown, set(), f"这些工具名不在官方清单里：{unknown}")

    def test_used_tools_count(self):
        self.assertEqual(len(USED_TOOLS), 9)

    def test_project_never_calls_write_tools(self):
        """只读原则：源码里不能出现下单/取消/领券/抽奖类工具名。"""
        sources = "\n".join(
            path.read_text(encoding="utf-8") for path in (ROOT / "src" / "time_to_mcd").glob("*.py")
        )
        # 常量定义里允许出现名字（用于说明"不使用"），但调用点不应引用
        for tool in WRITE_TOOLS:
            calls = re.findall(rf"self\.box\.call\(\s*[\"']{tool}[\"']", sources)
            self.assertEqual(calls, [], f"源码中出现对写操作工具的调用：{tool}")

    def test_mock_catalog_covers_official_list(self):
        mock_tools = set(MockBox().tools)
        missing = OFFICIAL_TOOLS - mock_tools
        self.assertEqual(missing, set(), f"mock 工具清单缺少：{missing}")

    def test_mock_schema_declared_for_used_tools(self):
        box = MockBox()
        for tool in USED_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn("inputSchema", box.tools[tool])
                self.assertTrue(box.tools[tool]["description"])

    def test_config_example_has_no_secret(self):
        text = (ROOT / "mcp-config.example.json").read_text(encoding="utf-8")
        self.assertIn("${MCD_MCP_TOKEN}", text)
        self.assertIsNone(re.search(r"Bearer\s+[A-Za-z0-9]{16,}", text))


if __name__ == "__main__":
    unittest.main()
