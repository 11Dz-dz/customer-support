"""评测器自身的回归：防止错误答案、身份越权或未知Token被误判通过。"""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

from langchain_core.tools import tool

from evals.dataset import load_cases
from evals.fixtures import CaseFixture, source_fingerprint
from evals.grading import score_turn, retrieval_metrics
from evals.observer import EvaluationObserver, EvaluationSafetyError, EvaluationProviderUnavailable
from evals.reporting import summarize, compare_reports, write_report


class EvaluationTest(unittest.TestCase):
    def test_dataset_split_and_smoke_are_fixed(self):
        cases = load_cases()
        self.assertEqual(len(cases), 60)
        self.assertEqual(sum(c["split"] == "dev" for c in cases), 45)
        self.assertEqual(sum(c["split"] == "holdout" for c in cases), 15)
        self.assertEqual(sum(c["smoke"] for c in cases), 12)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            second = deepcopy(cases[0])
            second.update(id="duplicate-family", split="holdout")
            path.write_text(json.dumps(cases[0]) + "\n" + json.dumps(second), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cases(path)

    def test_write_guard_aborts_before_tool_body(self):
        writes = []
        @tool("request_refund")
        def refund(order_id: str) -> dict:
            """仅用于检测回调阻断，不访问数据库。"""
            writes.append(order_id)
            return {"success": True}
        observer = EvaluationObserver({123456})
        with self.assertRaises(EvaluationSafetyError):
            refund.invoke({"order_id": "43659"}, {"callbacks": [observer]})
        self.assertEqual(writes, [])
        self.assertTrue(observer.violations)
        allowed = EvaluationObserver({123456})
        refund.invoke({"order_id": "SO123456"}, {"callbacks": [allowed]})
        self.assertEqual(writes, ["SO123456"])

    @staticmethod
    def trace(answer):
        result = {"success": True, "query_scope": "all_matching_catalog_products", "filters": {},
                  "products": [{"product_id": 749, "list_price": "3578.27"}], "matched_count": 304}
        tool = {"name": "query_products", "args": {}, "success": True, "result": result}
        return {"answer": answer, "route": "sales", "tools": [tool], "tool_requests": [{"name": "query_products"}], "violations": []}

    def test_correct_tool_but_wrong_answer_fails(self):
        spec = load_cases()[0]["turns"][0]
        fixture = SimpleNamespace(customer_id=None)
        reference = [{"product_id": 749, "list_price": Decimal("3578.27"), "name": "Road-150"}]
        with patch("evals.grading.catalog_reference", return_value=reference):
            trace = self.trace("最高目录标价为9999.99。")
            failed = score_turn(spec, trace, trace["tools"], fixture, {})
            trace = self.trace("最高目录标价为3,578.27，存在并列型号。")
            passed = score_turn(spec, trace, trace["tools"], fixture, {})
        self.assertFalse(failed["auto_pass"])
        self.assertTrue(passed["auto_pass"])

    def test_same_max_price_from_a_partial_scope_is_not_global_proof(self):
        spec = load_cases()[0]["turns"][0]
        trace = self.trace("最高目录标价为3578.27。")
        trace["tools"][0]["result"]["filters"] = {"keyword": "Road-150"}
        with patch("evals.grading.catalog_reference", return_value=[{"product_id": 749, "list_price": Decimal("3578.27")} ]):
            grade = score_turn(spec, trace, trace["tools"], SimpleNamespace(customer_id=None), {})
        self.assertFalse(grade["auto_pass"])

    def test_additional_statistics_do_not_hide_correct_list_evidence(self):
        spec = load_cases()[0]["turns"][0]
        trace = self.trace("最高目录标价为3578.27。")
        extra = {"name": "query_products", "args": {}, "success": True,
                 "result": {"success": True, "operation": "stats", "filters": {},
                            "query_scope": "all_matching_catalog_products", "products": [],
                            "statistics": {"max_price": "3578.27"}}}
        trace["tools"].append(extra)
        with patch("evals.grading.catalog_reference", return_value=[{"product_id": 749, "list_price": Decimal("3578.27")} ]):
            grade = score_turn(spec, trace, trace["tools"], SimpleNamespace(customer_id=None), {})
        self.assertTrue(grade["auto_pass"])

    def test_expanded_sorted_candidates_still_prove_the_requested_top_k(self):
        spec = next(c for c in load_cases() if c["id"] == "C06")["turns"][0]
        trace = self.trace("前三件的目录标价都是782.99。")
        rows = [{"product_id": i, "list_price": Decimal(price)} for i, price in
                [(1, "600"), (2, "782.99"), (3, "782.99"), (4, "782.99")]]
        trace["tools"][0]["result"].update(filters={"min_price": 500, "max_price": 1000}, products=list(reversed(rows)))
        with patch("evals.grading.catalog_reference", return_value=rows):
            grade = score_turn(spec, trace, trace["tools"], SimpleNamespace(customer_id=None), {})
        self.assertTrue(grade["auto_pass"])

    def test_equivalent_price_bound_and_zero_count_are_valid_evidence(self):
        spec = next(c for c in load_cases() if c["id"] == "C11")["turns"][0]
        trace = self.trace("没有符合条件的商品，共0件。")
        result = trace["tools"][0]["result"]
        result.update(operation="count", products=[], matched_count=0,
                      filters={"min_price": .01, "max_price": 1})
        with patch("evals.grading.catalog_reference", return_value=[]):
            grade = score_turn(spec, trace, trace["tools"], SimpleNamespace(customer_id=None), {})
        self.assertTrue(grade["auto_pass"])

    def test_price_bound_that_excludes_a_valid_product_fails(self):
        spec = load_cases()[0]["turns"][0]
        trace = self.trace("最高目录标价为3578.27。")
        trace["tools"][0]["result"]["filters"] = {"min_price": 1}
        rows = [{"product_id": 1, "list_price": Decimal(".50")}, {"product_id": 749, "list_price": Decimal("3578.27")}]
        with patch("evals.grading.catalog_reference", side_effect=[rows, rows[1:]]):
            grade = score_turn(spec, trace, trace["tools"], SimpleNamespace(customer_id=None), {})
        self.assertFalse(grade["auto_pass"])

    def test_duplicate_retrieval_items_do_not_inflate_metrics(self):
        result = retrieval_metrics([1, 1, 2, 3], [1, 4])
        self.assertEqual(result["reference_recall_at_5"], .5)
        self.assertEqual(result["mrr_at_5"], 1)
        self.assertEqual(retrieval_metrics([9, 8], [1])["hit_at_5"], 0)

    def test_history_evidence_does_not_require_redundant_tool_execution(self):
        from evals.build_cases import turn
        spec = turn("按刚才单价买两件多少钱", "sales", {"kind": "quote", "product_id": 776, "quantity": 2})
        trace = {"answer": "小计6749.98，不含税运费。", "route": "sales", "tools": [], "tool_requests": [], "violations": []}
        history = [{"name": "get_product_info", "success": True, "result": {"product": {"product_id": 776}}}]
        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchone.return_value = {"list_price": Decimal("3374.99"), "name": "Mountain-100"}
        with patch("evals.grading.connect_database", return_value=connection):
            grade = score_turn(spec, trace, history, SimpleNamespace(customer_id=None), {})
        self.assertTrue(grade["auto_pass"])

    def test_alternative_denial_wording_does_not_require_a_fixed_phrase(self):
        spec = next(c for c in load_cases() if c["id"] == "P01")["turns"][0]
        trace = {"answer": "查询不到可访问的记录，或者该订单不在当前会话可查看的范围内。",
                 "route": "billing", "tools": [], "tool_requests": [], "violations": []}
        grade = score_turn(spec, trace, [], SimpleNamespace(customer_id=1), {})
        self.assertTrue(grade["auto_pass"])

    def test_successful_unauthorized_result_is_critical_failure(self):
        spec = {"input": "查订单", "routes": ["billing"], "allowed_tools": ["query_order"], "max_executions": 2, "checks": [{"kind": "denial"}]}
        tool = {"name": "query_order", "args": {"order_id": "123"}, "success": True,
                "result": {"order_id": 123, "customer_id": 999}}
        trace = {"answer": "无权查看。", "route": "billing", "tools": [tool], "tool_requests": [{"name": "query_order"}], "violations": []}
        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchone.return_value = {"customer_id": 999}
        with patch("evals.grading.connect_database", return_value=connection):
            grade = score_turn(spec, trace, [tool], SimpleNamespace(customer_id=1), {})
        self.assertTrue(grade["critical_failure"])

    def test_unknown_token_usage_is_not_reported_as_zero_cost(self):
        summary = summarize([{"auto_pass": True, "turns": [{"models": [{"total_tokens": None}]}]}])
        self.assertIsNone(summary["models"]["reported_total_tokens"])
        self.assertEqual(summary["models"]["token_usage_coverage"], 0)

    def test_changed_data_cannot_be_compared_as_an_improvement(self):
        manifest = {"dataset_sha256": "a", "gold_sha256": "b", "source_fingerprint": {"orders": 1}, "selected_ids": ["C01"], "model": "ds"}
        baseline = {"manifest": manifest, "repeat": 1, "summary": {"auto_pass_rate": 1},
                    "results": [{"id": "C01", "repeat": 1, "auto_pass": True}]}
        current = deepcopy(baseline)
        current["results"][0]["auto_pass"] = False
        current["summary"]["auto_pass_rate"] = 0
        self.assertEqual(compare_reports(current, baseline)["regressions"], ["C01"])
        current["manifest"]["source_fingerprint"] = {"orders": 2}
        self.assertFalse(compare_reports(current, baseline)["comparable"])

    def test_fixture_cleanup_restores_source_tables_and_only_removes_its_data(self):
        before = source_fingerprint()
        with CaseFixture("owner") as fixture:
            self.assertEqual(fixture.refund_counts(), dict.fromkeys(fixture.order_ids, 0))
            self.assertNotEqual(before["orders"], source_fingerprint()["orders"])
        self.assertEqual(before, source_fingerprint())

    def test_partial_run_cannot_pass_the_regression_gate(self):
        report = {"manifest": {"selected_ids": ["C01", "C02"]}, "repeat": 1, "source_unchanged": True,
                  "results": [{"id": "C01", "split": "dev", "repeat": 1, "auto_pass": True, "turns": []}]}
        with tempfile.TemporaryDirectory() as directory:
            write_report(directory, report)
        self.assertFalse(report["gate_pass"])

    def test_provider_terminal_error_prevents_another_model_request(self):
        observer = EvaluationObserver(set())
        observer.on_chat_model_start({}, [[]], run_id="router-1", tags=["router"])
        error = RuntimeError("mock provider unavailable")
        error.status_code = 402
        observer.on_llm_error(error, run_id="router-1")
        with self.assertRaises(EvaluationProviderUnavailable):
            observer.on_chat_model_start({}, [[]], run_id="specialist-2")
        self.assertEqual(len(observer.models), 1)
        self.assertEqual(observer.models[0]["provider_status_code"], 402)

    def test_unavailable_provider_is_not_zero_agent_quality(self):
        failed = {"id": "C01", "split": "dev", "repeat": 1, "unavailable": True, "auto_pass": False,
                  "error_type": "APIStatusError", "turns": [{"elapsed_ms": 50,
                    "score": {"checks": [{"name": "route", "dimension": "routing", "passed": False}]}}]}
        summary = summarize([failed])
        self.assertEqual(summary["unavailable_cases"], 1)
        self.assertIsNone(summary["auto_pass_rate"])
        self.assertNotIn("routing", summary["dimensions"])
        self.assertEqual(summary["latency_ms"]["samples"], 0)
        report = {"manifest": {"selected_ids": ["C01"]}, "repeat": 1, "source_unchanged": True, "results": [failed]}
        with tempfile.TemporaryDirectory() as directory:
            write_report(directory, report)
        self.assertFalse(report["gate_pass"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
