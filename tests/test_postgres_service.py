"""真实 PostgreSQL 的业务集成检查；售后测试使用独立临时订单并在结束后清理。"""
import concurrent.futures
from decimal import Decimal
import unittest

from adventureworks_service import AdventureWorksService
from postgres_config import connect_database


class ServiceIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.service = AdventureWorksService()
        with connect_database() as conn:
            self.customer_id = conn.execute("SELECT customer_id FROM orders WHERE order_id=43659").fetchone()["customer_id"]

    def test_order_and_catalog_share_product(self):
        order = self.service.get_order("SO43659", customer_id=self.customer_id)
        self.assertTrue(order["success"])
        item = next(i for i in order["items"] if i["product_id"] == 776)
        product = self.service.get_product("776")["product"]
        self.assertEqual(item["product_name"], product["name"])
        self.assertEqual(item["description_en"], product["description_en"])
        self.assertTrue(item["description_zh"])
        self.assertNotEqual(Decimal(item["unit_price"]), Decimal(product["list_price"]))

    def test_unavailable_payment_and_invoice_are_explicit(self):
        self.assertEqual(self.service.get_payment("43659", customer_id=self.customer_id)["payment_status"], "unknown")
        self.assertEqual(self.service.get_invoice("43659", customer_id=self.customer_id)["invoice_status"], "not_available")
        self.assertIsNone(self.service.get_invoice("43659", customer_id=self.customer_id)["currency"])

    def test_search_and_quantity_estimate(self):
        result = self.service.search_products("mountain bike")
        self.assertGreater(len(result["products"]), 0)
        self.assertTrue(self.service.search_products("山地")['success'])
        product = self.service.get_product("776")["product"]
        quote = self.service.calculate_price("776", 2)
        self.assertEqual(Decimal(quote["subtotal"]), 2 * Decimal(product["list_price"]))
        self.assertFalse(self.service.calculate_price("776", -1)["success"])
        self.assertEqual(self.service.get_promotion("776")["promotion_status"], "unknown")

    def test_bad_identifiers_and_wildcards_do_not_expand_queries(self):
        self.assertFalse(self.service.get_order("43659 OR 1=1", customer_id=self.customer_id)["success"])
        self.assertFalse(self.service.get_order("999999999999999999999", customer_id=self.customer_id)["success"])
        self.assertEqual(self.service.search_products("zz_not_a_product' OR 1=1 --")["products"], [])
        self.assertEqual(self.service.search_products("%")["products"], [])

    def test_refund_is_deduplicated_under_concurrent_requests(self):
        # 使用专用测试订单，绝不删除或改写导入的样例订单/真实售后记录。
        test_id = 2147483601
        with connect_database() as conn:
            exists = conn.execute("SELECT 1 FROM orders WHERE order_id=%s", (test_id,)).fetchone()
            self.assertIsNone(exists, "测试订单 ID 已被占用，停止以免影响已有数据")
            conn.execute(
                """INSERT INTO orders(order_id,order_number,customer_id,status_code,purchase_at,due_at,
                                      subtotal,tax_amount,freight,total_due)
                   VALUES (%s,%s,1,5,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,0,0,0,0)""",
                (test_id, "TEST-PG-INTEGRATION"),
            )
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: self.service.request_refund(str(test_id), "集成测试申请", customer_id=1), range(4)))
            self.assertTrue(all(r["success"] for r in results))
            self.assertEqual(len({r["refund_id"] for r in results}), 1)
            self.assertTrue(all(r["status"] == "pending" for r in results))
        finally:
            with connect_database() as conn:
                conn.execute("DELETE FROM refund_requests WHERE order_id=%s", (test_id,))
                conn.execute("DELETE FROM orders WHERE order_id=%s AND order_number=%s",
                             (test_id, "TEST-PG-INTEGRATION"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
