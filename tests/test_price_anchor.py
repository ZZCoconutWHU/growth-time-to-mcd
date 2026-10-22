"""营养库查不到的商品：先拆套餐子项，再按「同类别 + 价格最接近」估算。

麦当劳营养库只有营养、没有价格；订单明细只有价格、没有营养。
两边靠商品名对上之后沉淀成「价格锚点」，以后遇到新品才有估算依据。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from time_to_mcd import nutrition as nut  # noqa: E402
from time_to_mcd.service import CompassService  # noqa: E402
from time_to_mcd.store import RecordStore  # noqa: E402
from time_to_mcd.wait import Order, _extract_items  # noqa: E402


class _NoBox:
    """营养库在测试里直接塞 _catalog，不需要真调 MCP。"""

    def has(self, name):
        return False

    def call(self, name, **kwargs):
        raise AssertionError(f"测试里不该真的调 MCP：{name}")


def _food(name, kcal, protein=10.0, fat=5.0, carb=20.0, sodium=300.0):
    return nut.Food(
        name=name,
        code="",
        energy=kcal,
        protein=protein,
        fat=fat,
        carb=carb,
        sodium=sodium,
        calcium=0.0,
        category="",
        unit="",
        raw={},
    )


class CategoryTests(unittest.TestCase):
    def test_burger_and_drink_and_dessert(self):
        self.assertEqual(nut.guess_category("新加坡蟹酱风味海鲜堡三件套"), "beef")
        self.assertEqual(nut.guess_category("圆筒冰淇淋"), "dessert")
        self.assertEqual(nut.guess_category("中杯可乐"), "drink")
        self.assertEqual(nut.guess_category("麦乐鸡"), "chicken")
        self.assertEqual(nut.guess_category("薯条"), "side")
        self.assertEqual(nut.guess_category("猪柳麦满分"), "breakfast")

    def test_unknown_falls_to_other(self):
        self.assertEqual(nut.guess_category("神秘新品"), "other")


class PriceKeptTests(unittest.TestCase):
    def test_order_items_keep_price(self):
        raw = {"orderProductList": [{"productName": "圆筒冰淇淋", "quantity": 1, "price": "5"}]}
        items = _extract_items(raw)
        self.assertEqual(items[0]["price"], 5.0)

    def test_combo_child_price_kept(self):
        raw = {
            "orderProductList": [
                {
                    "productName": "超值午餐",
                    "quantity": 1,
                    "price": "24",
                    "comboItemList": [{"productName": "板烧鸡腿堡", "quantity": 1, "price": "12"}],
                }
            ]
        }
        items = _extract_items(raw)
        self.assertEqual(items[0]["price"], 24.0)
        self.assertEqual(items[0]["combo"][0]["price"], 12.0)


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).with_name("_tmp_anchor.db")
        if self.path.exists():
            self.path.unlink()
        self.store = RecordStore(self.path)
        self.service = CompassService(_NoBox(), records=self.store)

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def _order_with(self, items):
        return Order(order_id="X1", store_name="测试餐厅", items=items)

    def test_exact_match_is_not_estimated(self):
        self.service._catalog = [_food("圆筒冰淇淋", 93)]
        result = self.service.order_nutrition(self._order_with([{"name": "圆筒冰淇淋", "qty": 1, "price": 5}]))
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["estimates"], [])
        self.assertAlmostEqual(result["kcal"], 93.0)

    def test_combo_children_used_before_estimate(self):
        """套餐名查不到 → 拆子项（真实值），用不上估算。"""
        self.service._catalog = [_food("板烧鸡腿堡", 400), _food("中薯条", 320)]
        result = self.service.order_nutrition(
            self._order_with(
                [
                    {
                        "name": "超值午餐三件套",
                        "qty": 1,
                        "price": 24,
                        "combo": [{"name": "板烧鸡腿堡", "qty": 1}, {"name": "中薯条", "qty": 1}],
                    }
                ]
            )
        )
        self.assertEqual(result["matched"], 1)   # 套餐算一项
        self.assertEqual(result["estimates"], [])
        self.assertAlmostEqual(result["kcal"], 720.0)

    def test_partial_combo_still_estimates_the_rest(self):
        """套餐里只有一部分子项查得到时，剩下那部分不能悄悄丢掉。"""
        self.store.upsert_food_anchor("麦香鸡", "beef", 12.0, 350.0, 15, 18, 30, 600)
        self.service._catalog = [_food("中薯条", 320)]
        result = self.service.order_nutrition(
            self._order_with(
                [
                    {
                        "name": "季节限定堡三件套",
                        "qty": 1,
                        "price": 30,
                        "combo": [{"name": "季节限定堡", "qty": 1}, {"name": "中薯条", "qty": 1}],
                    }
                ]
            )
        )
        self.assertEqual(result["matched"], 1)
        # 中薯条真实值 + 限定堡按 15 元（30 均摊）在汉堡类里找最近的麦香鸡
        self.assertAlmostEqual(result["kcal"], 670.0)
        self.assertEqual(len(result["estimates"]), 1)
        self.assertEqual(result["estimates"][0]["based_on"], "麦香鸡")

    def test_estimated_by_nearest_price_in_same_category(self):
        """子项也查不到 → 按同类 + 价格最接近的锚点估算，并如实标注。"""
        # 先喂一个锚点：麦香鸡 12 元 / 350 kcal（汉堡类）
        self.store.upsert_food_anchor("麦香鸡", "beef", 12.0, 350.0, 15, 18, 30, 600)
        self.service._catalog = []  # 营养库里什么都没有
        result = self.service.order_nutrition(
            self._order_with([{"name": "新加坡蟹酱风味海鲜堡", "qty": 1, "price": 11}])
        )
        self.assertEqual(result["matched"], 0)
        self.assertEqual(len(result["estimates"]), 1)
        est = result["estimates"][0]
        self.assertEqual(est["based_on"], "麦香鸡")
        self.assertEqual(est["category"], "beef")
        self.assertAlmostEqual(result["kcal"], 350.0)
        self.assertEqual(result["misses"], [])

    def test_no_anchor_means_no_fabricated_number(self):
        """冷启动没有锚点时，宁可标成未知也不编数。"""
        self.service._catalog = []
        result = self.service.order_nutrition(
            self._order_with([{"name": "不存在的汉堡", "qty": 1, "price": 11}])
        )
        self.assertEqual(result["estimates"], [])
        self.assertEqual(result["misses"], ["不存在的汉堡"])
        self.assertAlmostEqual(result["kcal"], 0.0)

    def test_anchor_persisted_from_matched_food(self):
        """匹配到营养库的商品要把「价格 → 营养」沉淀下来。"""
        self.service._catalog = [_food("圆筒冰淇淋", 93)]
        self.service.order_nutrition(self._order_with([{"name": "圆筒冰淇淋", "qty": 1, "price": 5}]))
        anchors = self.store.food_anchors(category="dessert")
        self.assertEqual(len(anchors), 1)
        self.assertAlmostEqual(anchors[0]["price"], 5.0)
        self.assertAlmostEqual(anchors[0]["kcal"], 93.0)

    def test_no_price_means_no_estimate(self):
        """没有价格就没法「按价格最近」，退回未知。"""
        self.store.upsert_food_anchor("麦香鸡", "beef", 12.0, 350.0, 15, 18, 30, 600)
        self.service._catalog = []
        result = self.service.order_nutrition(self._order_with([{"name": "神秘汉堡", "qty": 1}]))
        self.assertEqual(result["estimates"], [])
        self.assertEqual(result["misses"], ["神秘汉堡"])


if __name__ == "__main__":
    unittest.main()
