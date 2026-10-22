"""精确商品查询与预算过滤；直接和完整业务表核对，不依赖模型回答。"""
from decimal import Decimal
import json
import unittest

from adventureworks_service import AdventureWorksService
from postgres_config import connect_database


class ProductQueryTest(unittest.TestCase):
    def setUp(self):
        self.service = AdventureWorksService()

    def test_extremes_match_entire_catalog_and_report_ties(self):
        with connect_database() as conn:
            truth = conn.execute("SELECT min(list_price) AS lo,max(list_price) AS hi "
                                 "FROM product_catalog WHERE list_price>0").fetchone()
            ties = conn.execute("SELECT count(*) AS n FROM product_catalog WHERE list_price=%s",
                                (truth["hi"],)).fetchone()["n"]
        high = self.service.query_products(sort_order="desc", limit=1)
        low = self.service.query_products(sort_order="asc", limit=1)
        self.assertEqual(Decimal(high["products"][0]["list_price"]), truth["hi"])
        self.assertEqual(Decimal(low["products"][0]["list_price"]), truth["lo"])
        self.assertEqual(high["boundary_price_tie_count"], ties)
        self.assertIsNone(high["currency"])

    def test_filter_sort_and_count_are_applied_before_limit(self):
        result = self.service.query_products(min_price=500, max_price=1000, sort_order="desc", limit=3)
        with connect_database() as conn:
            rows = conn.execute("SELECT product_id FROM product_catalog WHERE list_price BETWEEN 500 AND 1000 "
                                "ORDER BY list_price DESC,product_id LIMIT 3").fetchall()
            count = conn.execute("SELECT count(*) AS n FROM product_catalog WHERE list_price BETWEEN 500 AND 1000").fetchone()["n"]
        self.assertEqual([r["product_id"] for r in result["products"]], [r["product_id"] for r in rows])
        self.assertEqual(result["matched_count"], count)

    def test_statistics_and_zero_price_policy(self):
        stats = self.service.query_products(operation="stats")
        with connect_database() as conn:
            truth = conn.execute("SELECT count(*) AS n, avg(list_price) AS average FROM product_catalog WHERE list_price>0").fetchone()
            total = conn.execute("SELECT count(*) AS n FROM product_catalog WHERE list_price>=0").fetchone()["n"]
        self.assertEqual(stats["matched_count"], truth["n"])
        self.assertEqual(Decimal(stats["statistics"]["average_price"]), truth["average"])
        self.assertEqual(self.service.query_products(operation="count", include_zero_price=True)["matched_count"], total)
        self.assertEqual(self.service.query_products(max_price=0)["products"], [])

    def test_keyword_color_size_and_invalid_parameters(self):
        result = self.service.query_products(keyword="Road-150", color="Red", size="44", limit=20)
        self.assertTrue(result["products"])
        self.assertTrue(all("road-150" in p["name"].lower() and p["color"] == "Red" and p["size"] == "44"
                            for p in result["products"]))
        for params in ({"min_price": 100, "max_price": 1}, {"max_price": float("nan")},
                       {"min_price": float("inf")}, {"min_price": -1}, {"limit": 1000},
                       {"sort_by": "list_price; DROP TABLE products"}, {"keyword": ""}):
            self.assertFalse(self.service.query_products(**params)["success"], params)
        for keyword in ("%", "zz_not_a_product' OR 1=1 --"):
            self.assertEqual(self.service.query_products(keyword=keyword)["matched_count"], 0)

    def test_hybrid_recall_filters_prices_before_ranking(self):
        result = self.service.search_products("适合长途骑行的自行车", min_price=500, max_price=1000, limit=10)
        self.assertEqual(result["retrieval_mode"], "hybrid")
        self.assertTrue(result["products"])
        # 模型工具接口必须返回 JSON，不能退化为含 Decimal/datetime 的 Python repr。
        self.assertEqual(json.loads(json.dumps(result))["returned_count"], len(result["products"]))
        self.assertTrue(all(Decimal("500") <= Decimal(p["list_price"]) <= Decimal("1000") for p in result["products"]))
        self.assertEqual(self.service.search_products("bike", max_price=0)["products"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
