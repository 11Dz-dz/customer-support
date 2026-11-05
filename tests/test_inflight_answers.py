"""Mutation tests: receipt/valid task parameters cannot hide an incorrect final answer."""
from copy import deepcopy
import unittest

from evals.inflight_answers import catalog_oracle,product_answer_checks,task_checks,outcome


class InflightAnswerTests(unittest.TestCase):
    def oracle(self,empty=False):
        return {'budget':'1000','quantity':1,'products':[] if empty else [{
            'product_id':1,'name':'Bicycle','product_subcategory_id':1,'list_price':'700.00',
            'description_zh':'测试自行车','description_en':'Test bicycle'}]}

    def test_correct_source_id_name_price_and_count_pass(self):
        text='商品1：Bicycle。\n历史目录标价：700.0000。\n目录描述原文：“测试自行车”'
        self.assertTrue(outcome(product_answer_checks(text,self.oracle()))['passed'])

    def test_generic_fallback_fails_even_if_catalog_is_empty(self):
        text='已检索到候选，但本次未能根据描述确认足够符合需求的推荐。请补充或缩小用途。'
        for empty in [False,True]:self.assertFalse(outcome(product_answer_checks(text,self.oracle(empty)))['passed'])

    def test_zero_match_answer_must_be_verified_by_sql(self):
        text='没有符合当前500预算条件的自行车整车。'
        self.assertTrue(outcome(product_answer_checks(text,self.oracle(True)))['passed'])
        self.assertFalse(outcome(product_answer_checks(text,self.oracle(False)))['passed'])

    def test_wrong_or_unknown_id_fails(self):
        checks=product_answer_checks('商品9：Bicycle。\n历史目录标价：700。',self.oracle())
        self.assertIn('selected_id_matches_independent_sql',outcome(checks)['failed_checks'])

    def test_wrong_price_fails_even_when_budget_is_met(self):
        checks=product_answer_checks('商品1：Bicycle。\n历史目录标价：600。',self.oracle())
        self.assertIn('rendered_price_matches_source',outcome(checks)['failed_checks'])

    def test_wrong_product_name_fails(self):
        checks=product_answer_checks('商品1：Helmet。\n历史目录标价：700。',self.oracle())
        self.assertIn('selected_name_matches_source',outcome(checks)['failed_checks'])

    def test_duplicate_products_do_not_satisfy_one_requested_product(self):
        text='商品1：Bicycle。\n历史目录标价：700。\n'
        self.assertIn('requested_product_count',outcome(product_answer_checks(text*2,self.oracle()))['failed_checks'])

    def test_unparseable_price_is_coverage_failure(self):
        checks=product_answer_checks('商品1：Bicycle。\n价格七百。',self.oracle())
        self.assertIn('rendered_price_matches_source',outcome(checks)['failed_checks'])

    def test_invented_description_is_rejected(self):
        text='商品1：Bicycle。\n历史目录标价：700。\n目录描述原文：“永久保修”'
        self.assertIn('description_quote_matches_source',outcome(product_answer_checks(text,self.oracle()))['failed_checks'])

    def test_no_catalog_result_cannot_be_explained_as_out_of_stock(self):
        checks=product_answer_checks('没有符合预算的自行车，已售罄。',self.oracle(True))
        self.assertIn('no_unverified_inventory_claim',outcome(checks)['failed_checks'])

    def test_correct_parameters_do_not_imply_business_answer_pass(self):
        tasks=[{'kind':'product_search','status':'empty','failure_code':None,
                'parameters':{'max_price':1000,'product_category':'bicycles','limit':1}}]
        self.assertTrue(outcome(task_checks(tasks,1000))['passed'])
        self.assertFalse(outcome(product_answer_checks('暂时无法可靠整理',self.oracle()))['passed'])

    def test_independent_sql_is_not_routed_through_search(self):
        class Connection:
            def execute(inner,sql,args):
                self.assertIn('product_subcategory_id IN (1,2,3)',sql)
                self.assertNotIn('LIMIT',sql);self.assertNotIn('embedding',sql)
                return inner
            def fetchall(inner):return []
        self.assertEqual(catalog_oracle(Connection(),500)['matched_count'],0)
