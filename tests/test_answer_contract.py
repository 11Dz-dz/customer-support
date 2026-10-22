"""关键事实、真实业务集成及未校验草稿隔离回归；不调用DeepSeek。"""
from decimal import Decimal
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from adventureworks_service import AdventureWorksService
from answer_contract import build_business_answer, verified_chunks


def human(text):
    return SimpleNamespace(type="human", content=text)


def tool(name, value):
    return SimpleNamespace(type="tool", name=name, content=json.dumps(value, ensure_ascii=False))


class AnswerContractTest(unittest.TestCase):
    def test_wrong_date_amount_and_currency_in_draft_never_become_answer(self):
        result = {"success": True, "order_id": 12, "status": "已发货", "status_code": 5,
                  "due_at": "2026-01-08T00:00:00", "total_due": "200.0000"}
        text, audit = build_business_answer([human("查询订单12"), tool("query_order", result)],
                                            '付款截止2020-01-01，总额9999元，已支付成功', "billing")
        self.assertIn("订单约定交付日期：2026-01-08", text)
        self.assertIn("订单应付总额：200.0000", text)
        self.assertNotIn("9999", text)
        self.assertNotIn("2020-01-01", text)
        self.assertNotIn("已支付成功", text)
        self.assertTrue(audit["plan_fallback"])
        focused, _ = build_business_answer([human("查询订单12的商品"), tool("query_order", result)],
                                            '{"focus":["items"]}', "billing")
        self.assertIn("订单状态：已发货", focused)

    def test_boundary_tie_does_not_change_unique_minimum(self):
        result = {"success": True, "operation": "list", "matched_count": 2, "filters": {},
                  "sort_by": "price", "sort_order": "asc", "products": [
                      {"product_id": 1, "name": "A", "list_price": "2.29"},
                      {"product_id": 2, "name": "B", "list_price": "4.99"}],
                  "extreme_kind": "min", "extreme_price": "2.29", "extreme_tie_count": 1,
                  "boundary_price": "4.99", "boundary_price_tie_count": 3}
        text, _ = build_business_answer([human("最低价"), tool("query_products", result)], '{}', "sales")
        self.assertIn("最低标价为2.29，对应1条", text)
        self.assertIn("该价位没有并列", text)
        self.assertIn("末项价位4.99", text)
        self.assertIn("共有3条同价", text)

    def test_created_existing_approved_and_history_are_distinct(self):
        result = {"success": True, "order_id": 12, "refund_id": "RF-test", "status": "pending", "already_exists": False}
        plan = '{"focus":["refund"]}'
        created, _ = build_business_answer([human("退款"), tool("request_refund", result)], plan, "billing")
        self.assertIn("已登记退款申请", created)
        result["already_exists"] = True
        existing, _ = build_business_answer([human("退款"), tool("request_refund", result)], plan, "billing")
        self.assertIn("本次复用，没有重复登记", existing)
        result["status"] = "approved"
        approved, _ = build_business_answer([human("退款"), tool("request_refund", result)], plan, "billing")
        self.assertIn("不代表资金已退回", approved)
        self.assertIn("尚未接入", approved)
        history, _ = build_business_answer([human("退款"), tool("request_refund", result), human("同一申请再提交")], plan, "billing")
        self.assertIn("本轮未重复提交", history)

    def test_error_and_new_order_never_fall_back_to_private_history(self):
        old = tool("query_order", {"success": True, "order_id": 12, "total_due": "200"})
        failed = tool("query_order", {"success": False, "error_code": "ORDER_NOT_ACCESSIBLE"})
        text, _ = build_business_answer([human("订单12"), old, human("订单13"), failed], '{"order_id":12}', "billing")
        self.assertIn("无权", text)
        self.assertNotIn("200", text)
        text, _ = build_business_answer([human("订单12"), old, human("订单13")], '{"order_id":12}', "billing")
        self.assertNotIn("200", text)

    def test_changed_budget_cannot_select_old_expensive_product(self):
        old = tool("search_products", {"success": True, "products": [{"product_id": 1, "name": "OLD", "list_price": "900"}]})
        new = tool("search_products", {"success": True, "products": [{"product_id": 2, "name": "NEW", "list_price": "100"}]})
        text, audit = build_business_answer([human("预算1000"), old, human("预算500"), new], '{"product_ids":[1]}', "sales")
        self.assertIn("NEW", text)
        self.assertNotIn("OLD", text)
        self.assertTrue(audit["plan_fallback"])

    def test_source_quote_is_exactly_validated(self):
        record = tool("get_product_info", {"success": True, "product": {"product_id": 1, "name": "车把", "description_en": "A light aluminum bar."}})
        plan = {"product_ids": [1], "explanations": [{"product_id": 1, "field": "description_en", "text": "Guaranteed waterproof, only 1元."}]}
        text, _ = build_business_answer([human("介绍"), record], json.dumps(plan), "sales")
        self.assertNotIn("Guaranteed waterproof", text)
        self.assertNotIn("1元", text)
        self.assertIn("A light aluminum bar.", text)

    def test_bundle_totals_are_recomputed_and_bad_evidence_is_rejected(self):
        result = {"success": True, "budget": "100", "bundles": [{"bundle_index": 1,
                  "items": [{"product_id": 915, "product_name": "车座", "quantity": 1, "unit_price": "39.14", "line_total": "39.14"},
                            {"product_id": 946, "product_name": "车把", "quantity": 1, "unit_price": "46.09", "line_total": "46.09"}],
                  "subtotal": "85.23", "within_budget": True}]}
        text, _ = build_business_answer([human("报价"), tool("calculate_price", result)], '{}', "sales")
        self.assertIn("85.23", text)
        self.assertIn("在预算内", text)
        result["bundles"][0]["subtotal"] = "130.63"
        with self.assertRaises(ValueError):
            build_business_answer([human("报价"), tool("calculate_price", result)], '{}', "sales")

    def test_policy_does_not_echo_previous_order(self):
        messages = [human("订单12"), tool("query_order", {"success": True, "order_id": 12, "total_due": "200"}), human("退款政策，不申请")]
        text, _ = build_business_answer(messages, '{"kind":"refund_policy"}', "billing")
        self.assertIn("没有可查询的退款政策", text)
        self.assertNotIn("200", text)

    def test_verified_chunks_preserve_exact_text(self):
        text = "【账单专员】\n订单约定交付日期：2026-01-08。\n" * 30
        parts = list(verified_chunks(text))
        self.assertGreater(len(parts), 1)
        self.assertEqual(''.join(parts), text)


class FactServiceIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.service = AdventureWorksService()

    def test_bundle_prices_budget_duplicates_and_input_validation(self):
        bundles = [{"items": [{"product_id": 915, "quantity": 1}, {"product_id": 946, "quantity": 1}]},
                   {"items": [{"product_id": 915, "quantity": 1}, {"product_id": 946, "quantity": 1}, {"product_id": 941, "quantity": 1}]}]
        result = self.service.calculate_bundles(bundles, "100")
        self.assertEqual([b["subtotal"] for b in result["bundles"]], ["85.2300", "166.2200"])
        self.assertEqual([b["within_budget"] for b in result["bundles"]], [True, False])
        same = self.service.calculate_bundles([{"items": [{"product_id": 915, "quantity": 1}]*2}])
        self.assertEqual(same["bundles"][0]["items"][0]["quantity"], 2)
        for bad in ([{"items": [{"product_id": 915, "quantity": True}]}],
                    [{"items": [{"product_id": 915, "quantity": 1, "unit_price": ".01"}]}],
                    [{"items": [{"product_id": 2147483647, "quantity": 1}]}]):
            self.assertFalse(self.service.calculate_bundles(bad)["success"])
        self.assertFalse(self.service.calculate_bundles(bundles, 'NaN')["success"])

    def test_dates_extreme_ties_and_refund_operation(self):
        from evals.fixtures import CaseFixture
        with CaseFixture("owner") as fixture:
            order = self.service.get_order(str(fixture.slots['own_order']), customer_id=fixture.customer_id)
            self.assertEqual(order['date_labels']['due_at'], '订单约定交付日期')
            self.assertEqual(order['date_semantics']['payment_due_at'], 'unknown')
            first = self.service.request_refund(str(order['order_id']), '测试', customer_id=fixture.customer_id)
            second = self.service.request_refund(str(order['order_id']), '测试', customer_id=fixture.customer_id)
            self.assertFalse(first['already_exists'])
            self.assertTrue(second['already_exists'])
            self.assertEqual(first['refund_id'], second['refund_id'])
            self.assertFalse(first['capabilities']['review_notification'])
        result = self.service.query_products(limit=5)
        self.assertEqual(result['extreme_tie_count'], 1)
        self.assertEqual(result['boundary_price_tie_count'], 3)
        self.assertEqual(Decimal(result['extreme_price']), Decimal('2.29'))
        self.assertIsNone(self.service.query_products(max_price=0)['extreme_tie_count'])

    def test_zero_catalog_price_cannot_be_quoted_as_free(self):
        from postgres_config import connect_database
        with connect_database() as conn:
            pid=conn.execute('SELECT product_id FROM product_catalog WHERE list_price=0 ORDER BY product_id LIMIT 1').fetchone()['product_id']
        self.assertFalse(self.service.calculate_price(str(pid),1)['success'])
        self.assertFalse(self.service.calculate_bundles([{'items':[{'product_id':pid,'quantity':1}]}])['success'])

    def test_real_graph_hides_streamed_draft_and_saves_only_verified_answer(self):
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        from langchain_core.messages import AIMessage, ToolMessage
        from evals.fixtures import CaseFixture
        from chat_runtime import stream_turn, invoke_turn
        import langgraph_supervisor_1 as project

        class FakeAgent:
            def invoke(self, state, config):
                wrong = FakeListChatModel(responses=['付款截止2020-01-01，总额9999元，已退款成功'], tags=['business_draft'])
                draft = wrong.invoke(state['messages'], config)
                result = project.query_order.invoke({'order_id': str(oid)}, config)
                return {'messages': state['messages'] + [
                    AIMessage(content='', tool_calls=[{'name':'query_order','args':{'order_id':str(oid)},'id':'verify-call'}]),
                    ToolMessage(content=json.dumps(result), name='query_order', tool_call_id='verify-call'), draft]}

        with CaseFixture('owner') as fixture:
            oid=fixture.slots['own_order']
            with patch.object(project, 'billing_agent', FakeAgent()), patch.object(project, 'supervisor_router') as router:
                router.invoke.return_value = project.RouteDecision(next='billing')
                parts=list(stream_turn(project.app, '查询订单'+str(oid), fixture.thread_id))
                rendered=''.join(p['text'] for p in parts if p['type']=='text')
                saved=project.app.get_state({'configurable':{'thread_id':fixture.thread_id}}).values['messages'][-1].content
                self.assertEqual(rendered,saved)
                self.assertIn('订单约定交付日期：2026-01-08',rendered)
                self.assertNotIn('9999元',rendered)
                self.assertNotIn('已退款成功',rendered)
                self.assertGreater(len([p for p in parts if p['type']=='text']),1)
                again=invoke_turn(project.app,'这个订单是什么状态',fixture.thread_id)
                self.assertIn('已发货',again['messages'][-1].content)


if __name__=='__main__':
    unittest.main(verbosity=2)
