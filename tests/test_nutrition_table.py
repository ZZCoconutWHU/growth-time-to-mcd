"""营养库解析：MCP 实际返回的是文本表格，不是 JSON 数组。

实测 list-nutrition-foods 的 data 形如：
    [160]{productName,nutritionDescription,energyKj,energyKcal,protein,...}:
      猪柳麦满分,null,1288,308,16,16,24,781,213
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from time_to_mcd import nutrition as nut  # noqa: E402

SAMPLE = """[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  猪柳麦满分,null,1288,308,16,16,24,781,213
  中薯条,null,1210,289,4,12,38,165,18
  无糖可口可乐中杯,null,0,0,0,0,1,35,0
"""


class ParseFoodTableTest(unittest.TestCase):
    def test_parses_rows_into_foods(self):
        foods = nut.parse_food_table(SAMPLE)
        self.assertEqual(len(foods), 3)
        first = foods[0]
        self.assertEqual(first.name, "猪柳麦满分")
        self.assertEqual(first.energy, 308.0)
        self.assertEqual(first.protein, 16.0)
        self.assertEqual(first.fat, 16.0)
        self.assertEqual(first.carb, 24.0)
        self.assertEqual(first.sodium, 781.0)
        self.assertEqual(first.calcium, 213.0)

    def test_keeps_zero_kcal_items(self):
        """无糖可乐、饮用水本就是 0 kcal，且是降卡推荐的关键选项，不能丢。"""
        foods = {f.name: f for f in nut.parse_food_table(SAMPLE)}
        self.assertIn("无糖可口可乐中杯", foods)
        self.assertEqual(foods["无糖可口可乐中杯"].energy, 0.0)

    def test_returns_empty_for_plain_text(self):
        self.assertEqual(nut.parse_food_table("没有表格头的普通文本"), [])

    def test_extract_foods_reads_table_inside_payload(self):
        payload = {"success": True, "code": 200, "data": SAMPLE}
        foods = nut.extract_foods(payload)
        self.assertEqual(len(foods), 3)
        self.assertEqual(foods[0].name, "猪柳麦满分")

    def test_extract_foods_still_handles_json_arrays(self):
        """不能因为加了表格解析就破坏原有的 JSON 结构解析。"""
        payload = {"data": [{"productName": "巨无霸", "energyKcal": 550, "protein": 30}]}
        foods = nut.extract_foods(payload)
        self.assertEqual(len(foods), 1)
        self.assertEqual(foods[0].energy, 550.0)


if __name__ == "__main__":
    unittest.main()
