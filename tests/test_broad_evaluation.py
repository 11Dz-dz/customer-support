"""The expanded evaluator must fail deliberately wrong recorded answers."""
import unittest
import json
import importlib.util
from datetime import datetime,timezone
from decimal import Decimal
from types import SimpleNamespace
from evals.broad_cases import broad_cases,turn
from evals.broad_contracts import score_broad,shown_products

class Cursor:
    def __init__(self,rows):self.rows=rows
    def fetchall(self):return self.rows
    def fetchone(self):return self.rows[0] if self.rows else None
class Connection:
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def execute(self,sql,args=None):
        if 'list_price FROM product_catalog WHERE product_id=ANY' in sql:return Cursor([{'product_id':989,'name':'Known bike','list_price':'539.9900'}])
        if 'SELECT * FROM orders' in sql:return Cursor([{'customer_id':2,'subtotal':200,'total_due':200}])
        return Cursor([{'product_id':989,'name':'Known bike','list_price':'539.9900','color':'Black','size':'40'}])
def fixture():return SimpleNamespace(customer_id=1,slots={},connect=lambda:Connection())
def trace(answer,tools=()):return {'answer':answer,'input':'test','tools':list(tools),'tool_requests':[],'task_execution':[]}
def checks(spec,result):return {c['name']:c['passed'] for c in score_broad(spec,result,fixture(),result['tools'])}

class BroadEvaluationTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('langchain_core'),'Server runtime SDK required')
    def test_named_fault_stub_reaches_the_real_exception_boundary(self):
        from business_tools import _business_tool
        calls=[]
        def fail_once(**kw):
            calls.append(kw)
            if len(calls)==1:raise TimeoutError('controlled')
            return {'success':True}
        first=_business_tool(fail_once,order_id=123)
        self.assertEqual(first['error_code'],'TOOL_TIMEOUT');self.assertTrue(first['retryable'])
        self.assertTrue(_business_tool(fail_once,order_id=123)['success']);self.assertEqual(len(calls),2)
    def test_judge_reference_serializes_decimal_and_datetime_before_sdk_call(self):
        from evals.broad_runner import ready_judge_reference
        value=ready_judge_reference({'orders':[{'date':datetime(2026,1,1,tzinfo=timezone.utc)}]},
            {'broad_reference':{'price':Decimal('539.9900')}},{})
        json.dumps(value)
        self.assertEqual(value['independent_broad_reference']['price'],'539.9900')
    def test_dataset_is_complete_and_all_slots_format(self):
        cases=broad_cases();self.assertEqual(len(cases),90);self.assertEqual(len({c['id'] for c in cases}),90)
        for case in cases:
            for row in case['turns']:
                row['input'].format(**{k:1 for k in ['own_order','foreign_order','missing_order','source_own','source_other','own_customer','other_customer']})
                self.assertTrue(row['contract']['broad']['expectation'])
        self.assertEqual(sum(len(c['turns']) for c in cases),129)

    def test_billing_sku_does_not_contaminate_sales_filter(self):
        self.assertEqual(shown_products('【销售顾问】商品989：Bike。\n【账单专员】商品776：Ordered item。'),[989])

    def test_wrong_id_is_rejected_even_with_correct_count(self):
        spec=turn('filter','filter',sql='TRUE');row=trace('商品999：wrong',[{'name':'query_products','success':True,'result':{'matched_count':1,'products':[]}}])
        self.assertFalse(checks(spec,row)['broad_displayed_ids_match_sql'])

    def test_a_correct_id_cannot_hide_a_wrong_full_count(self):
        spec=turn('count','count',sql='TRUE',mode='count');row=trace('共2条。商品989：Known bike。',[{'name':'query_products','success':True,'result':{'matched_count':2,'products':[]}}])
        self.assertFalse(checks(spec,row)['broad_complete_sql_count'])

    def test_wrong_quote_is_rejected_even_if_expected_number_appears(self):
        spec=turn('quote','quote',quote_items=[{'product_id':989,'quantity':2}]);row=trace('参考数字1079.98。商品小计：1.00。',[{'name':'calculate_price','success':True,'result':{'bundles':[{'subtotal':'1.00'}]}}])
        self.assertFalse(checks(spec,row)['broad_quote_total_matches_sql'])

    def test_scope_refusal_with_a_tool_execution_does_not_pass(self):
        from conversation_replies import REPLIES
        spec=turn('code','refuse',scope_refusal=True,no_tools=True)
        row=trace(REPLIES['out_of_scope'],[{'name':'query_products','success':True,'result':{}}])
        self.assertFalse(checks(spec,row)['broad_no_tools'])

    def test_foreign_order_is_rejected_without_trusting_omitted_customer_id(self):
        spec=turn('order','deny');row=trace('订单：123。',[{'name':'query_order','success':True,'result':{'order_id':123}}])
        self.assertFalse(checks(spec,row)['all_private_rows_authorized'])

    def test_faults_are_explicit_and_only_affect_two_controlled_cases(self):
        affected=[c['id'] for c in broad_cases() if c.get('fault')]
        self.assertEqual(affected,['EV_RX06','EV_RX07'])

if __name__=='__main__':unittest.main()
