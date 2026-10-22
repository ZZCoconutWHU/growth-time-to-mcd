"""历史订单回填「记录」屏。

真实结构踩坑记录（mock 掩盖不了）：
  - query-order 的明细字段叫 `orderProductList`，不叫 `productList`
  - `status` 是状态码（"40"），`orderStatus` 才是人话（"订单已完成"）
  - order-list 只给骨架，没有商品明细，要逐单用 query-order 补
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from time_to_mcd import wait as wat  # noqa: E402
from time_to_mcd.service import CompassService  # noqa: E402

ORDER_DETAIL = {
    "orderId": "1000000000000000000000000001",
    "orderStatus": "订单已完成",
    "status": "40",
    "storeName": "麦当劳示例餐厅（脱敏）",
    "orderProductList": [
        {"productName": "圆筒冰淇淋", "quantity": 1, "price": "5", "comboItemList": []}
    ],
    "totalAmount": "5",
    "createTime": "2026-08-29 12:33:53",
    "takeWay": "堂食",
    "pickupCode": "35160",
}


class ExtractItemsTests(unittest.TestCase):
    def test_reads_order_product_list(self):
        """字段名是 orderProductList，漏了就永远解析不出商品。"""
        items = wat._extract_items(ORDER_DETAIL)
        # price 是后来加的：营养库查不到的商品要靠它按「同类价格最近」估算
        self.assertEqual(items, [{"name": "圆筒冰淇淋", "qty": 1.0, "price": 5.0}])

    def test_combo_children_kept_for_fallback(self):
        """套餐名常常不在营养库里，子项要留着兜底。"""
        raw = {
            "orderProductList": [
                {
                    "productName": "超值午餐",
                    "quantity": 1,
                    "comboItemList": [
                        {"productName": "板烧鸡腿堡", "quantity": 1},
                        {"productName": "中薯条", "quantity": 1},
                    ],
                }
            ]
        }
        items = wat._extract_items(raw)
        self.assertEqual(items[0]["name"], "超值午餐")
        self.assertEqual([c["name"] for c in items[0]["combo"]], ["板烧鸡腿堡", "中薯条"])


class NormalizeOrderTests(unittest.TestCase):
    def test_status_text_not_status_code(self):
        order = wat.normalize_order(ORDER_DETAIL)
        self.assertEqual(order.status_text, "订单已完成")  # 不能是 "40"
        self.assertTrue(order.is_final)

    def test_store_and_time(self):
        order = wat.normalize_order(ORDER_DETAIL)
        self.assertEqual(order.store_name, "麦当劳示例餐厅（脱敏）")
        self.assertEqual(order.created_at.strftime("%Y-%m-%d %H:%M"), "2026-08-29 12:33")


class OrderNutritionTests(unittest.TestCase):
    def _service(self, order):
        svc = CompassService.__new__(CompassService)

        class Food:
            def __init__(self, name, energy, protein):
                self.name, self.energy, self.protein = name, energy, protein
                self.fat, self.carb, self.sodium = 0.0, 0.0, 0.0

        svc.catalog = lambda refresh=False: [
            Food("圆筒冰淇淋", 93, 2),
            Food("板烧鸡腿堡", 404, 26),
            Food("中薯条", 337, 4),
        ]
        return svc

    def test_matched_and_missed(self):
        order = wat.Order(order_id="1", items=[{"name": "圆筒冰淇淋", "qty": 2}, {"name": "火星汉堡", "qty": 1}])
        n = self._service(order).order_nutrition(order)
        self.assertEqual(n["kcal"], 186.0)  # 93 × 2
        self.assertEqual(n["matched"], 1)
        self.assertEqual(n["misses"], ["火星汉堡"])

    def test_combo_fallback_used(self):
        """套餐名匹配不到时，用子项累加，而不是当成 0。"""
        order = wat.Order(
            order_id="1",
            items=[{"name": "超值午餐", "qty": 1, "combo": [{"name": "板烧鸡腿堡", "qty": 1}, {"name": "中薯条", "qty": 1}]}],
        )
        n = self._service(order).order_nutrition(order)
        self.assertEqual(n["kcal"], 741.0)
        # 套餐按「一项」计数（子项只是它的拆分方式），不是按子项数
        self.assertEqual(n["matched"], 1)
        self.assertEqual(n["misses"], [])
        self.assertEqual([h["via"] for h in n["hits"]], ["combo", "combo"])

    def test_empty_order_is_zero_not_crash(self):
        order = wat.Order(order_id="1", items=[])
        n = self._service(order).order_nutrition(order)
        self.assertEqual(n["kcal"], 0.0)
        self.assertEqual(n["total"], 0)


class LoggedRefTests(unittest.TestCase):
    """同一单只能记一次：靠 meals.ref_id 去重。"""

    def test_ref_id_roundtrip(self):
        import sqlite3
        import tempfile

        from time_to_mcd.store import RecordStore

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            # 先建一个「旧库」（没有 source/ref_id 两列），验证迁移会补列
            old = sqlite3.connect(path)
            old.execute(
                """CREATE TABLE meals (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, store TEXT,
                   meal_type TEXT, items_json TEXT NOT NULL, kcal REAL NOT NULL DEFAULT 0,
                   protein REAL NOT NULL DEFAULT 0, fat REAL NOT NULL DEFAULT 0, carb REAL NOT NULL DEFAULT 0,
                   sodium REAL NOT NULL DEFAULT 0, target_kcal REAL, on_target INTEGER NOT NULL DEFAULT 0)"""
            )
            old.commit()
            old.close()

            store = RecordStore(path)
            self.assertEqual(store.logged_ref_ids(), set())
            store.log_meal(items=[], kcal=100, protein=5, fat=1, carb=10, sodium=50,
                           source="mcp-order", ref_id="ORDER-1")
            self.assertEqual(store.logged_ref_ids(), {"ORDER-1"})


if __name__ == "__main__":
    unittest.main()
