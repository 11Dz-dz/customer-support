"""No paid model needed: mutate facts, not the grader's implementation details."""
from copy import deepcopy
import json
import unittest

from evals.order_answer_facts import answer_fact_checks
from evals.judge_reference import (load_reference,public_judge_inputs,
                                 classification_metrics,render_review)


class OrderAnswerFactsTests(unittest.TestCase):
    def setUp(self):
        self.fixture,self.dataset=load_reference()
        self.refs={r['order_id']:r for r in self.fixture['orders'] if r['customer_id']==70001}

    def checks(self,text,required=None,default=None):
        return answer_fact_checks(text,self.refs,required or [],default_order=default)

    def test_all_authored_amount_examples_have_expected_verdicts(self):
        for case in self.dataset['cases']:
            if 'rule_expected' not in case:continue
            required=case['required'];targets={r['order_id'] for r in required}
            checks=self.checks(case['answer'],required,next(iter(targets)) if len(targets)==1 else None)
            with self.subTest(case=case['id']):self.assertIs(all(c['passed'] for c in checks),case['rule_expected'])

    def test_correct_number_in_product_id_does_not_satisfy_total(self):
        checks=self.checks('商品编号200，欢迎再次咨询。',[{'order_id':12345,'field':'total_due'}],12345)
        self.assertFalse(all(c['passed'] for c in checks))

    def test_cached_evidence_can_validate_answer_without_current_tool_call(self):
        checks=self.checks('刚才的订单，应付金额为200。',[{'order_id':12345,'field':'total_due'}],12345)
        self.assertTrue(all(c['passed'] for c in checks))

    def test_unknown_or_foreign_order_is_not_grounded(self):
        for oid in [54321,99999]:
            with self.subTest(order=oid):
                checks=self.checks(f'订单{oid}，应付总额900。')
                self.assertTrue(any(c['critical'] and not c['passed'] for c in checks))

    def test_multi_order_amounts_without_association_need_review(self):
        checks=self.checks('应付总额200。应付总额345.67。',
            [{'order_id':12345,'field':'total_due'},{'order_id':12346,'field':'total_due'}])
        self.assertFalse(all(c['passed'] for c in checks))
        self.assertTrue(any(c['name']=='amount_order_is_grounded' and not c['passed'] for c in checks))

    def test_markdown_and_rounding_do_not_reject_correct_fact(self):
        checks=self.checks('订单 **SO12346**\n**订单总金额**：345.67。\n**运费**：15.67。',
            [{'order_id':12346,'field':'total_due'}])
        self.assertTrue(all(c['passed'] for c in checks))

    def test_conflicting_claims_do_not_cancel_each_other(self):
        checks=self.checks('订单12345：应付总额200。应付总额999。',
            [{'order_id':12345,'field':'total_due'}])
        self.assertFalse(all(c['passed'] for c in checks))

    def test_negation_or_unknown_is_not_parsed_as_a_positive_numerical_claim(self):
        self.assertEqual(self.checks('订单12345的应付总额不是999，当前无法确认。'),[])
        self.assertFalse(all(c['passed'] for c in self.checks('订单12345的总额未知。',
            [{'order_id':12345,'field':'total_due'}])))

    def test_unsupported_wording_is_missing_coverage_instead_of_silent_pass(self):
        checks=self.checks('这笔共计两百。',[{'order_id':12345,'field':'total_due'}],12345)
        self.assertFalse(all(c['passed'] for c in checks))
        self.assertTrue(all(not c['critical'] for c in checks if not c['passed']))

    def test_labels_and_rationales_never_reach_judge(self):
        for case in self.dataset['cases']:
            mutated=deepcopy(case);mutated['expected']='DO_NOT_SEND_VERDICT';mutated['reason']='DO_NOT_SEND_REASON'
            payload=json.dumps(public_judge_inputs(mutated,self.fixture),ensure_ascii=False)
            self.assertNotIn('DO_NOT_SEND_VERDICT',payload);self.assertNotIn('DO_NOT_SEND_REASON',payload)

    def test_metrics_keep_false_accept_reject_and_unavailable_separate(self):
        results=[{'expected':False,'result':{'status':'scored','score':True}},
                 {'expected':True,'result':{'status':'scored','score':False}},
                 {'expected':True,'result':{'status':'unavailable','score':None}}]
        m=classification_metrics(results)
        self.assertEqual((m['false_accepts'],m['false_rejects'],m['unavailable']),(1,1,1))
        self.assertAlmostEqual(m['coverage'],2/3)

    def test_review_material_is_truthful_about_source_and_author(self):
        text=render_review()
        self.assertIn('尚无独立真人标注',text)
        self.assertEqual(text.count('**你的复核**'),30)
        self.assertIn('不是生产数据库导出',text)

    def test_current_score_uses_historical_order_and_requires_requested_total(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        from evals.current_contracts import score_current
        refs=self.refs
        class Connection:
            def execute(self,sql,args):return SimpleNamespace(fetchone=lambda:refs.get(args[0]))
        @contextmanager
        def connect():yield Connection()
        fixture=SimpleNamespace(connect=connect,customer_id=70001,slots={'own_order':12345})
        spec={'checks':[{'kind':'order','slot':'own_order','fact':'total'}],'contract':{}}
        trace={'answer':'应付金额为200。','tools':[],'task_execution':[{'parameters':{'order_id':12345}}]}
        history=[{'name':'query_order','success':True,'result':refs[12345]}]
        self.assertTrue(all(c['passed'] for c in score_current(spec,trace,fixture,history)))
        for text in ['商品编号200，总额不知道。','订单12345：应付总额999。']:
            trace['answer']=text
            with self.subTest(answer=text):self.assertFalse(all(c['passed'] for c in score_current(spec,trace,fixture,history)))

    def test_current_score_rejects_cross_order_swap_even_without_total_requirement(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        from evals.current_contracts import score_current
        refs=self.refs
        class Connection:
            def execute(self,sql,args):return SimpleNamespace(fetchone=lambda:refs.get(args[0]))
        @contextmanager
        def connect():yield Connection()
        fixture=SimpleNamespace(connect=connect,customer_id=70001,slots={})
        trace={'answer':'订单12345，应付总额345.67。订单12346，应付总额200。','tools':[],
               'task_execution':[{'parameters':{'order_id':12345}},{'parameters':{'order_id':12346}}]}
        history=[{'name':'query_order','success':True,'result':r} for r in refs.values()]
        checks=score_current({'checks':[],'contract':{}},trace,fixture,history)
        self.assertTrue(any(c['critical'] and not c['passed'] for c in checks))

    def test_product_quote_is_not_an_order_subtotal_even_with_old_order_in_history(self):
        from types import SimpleNamespace
        from evals.current_contracts import score_current
        fixture=SimpleNamespace(customer_id=70001,slots={},connect=lambda:(_ for _ in ()).throw(AssertionError('No order SQL needed')))
        trace={'answer':'商品小计：6749.98，不含税和运费。','tools':[],
               'task_execution':[{'kind':'quote','parameters':{'product_ids':[776],'quantity':2}}]}
        history=[{'name':'query_order','success':True,'result':self.refs[12345]}]
        checks=score_current({'checks':[],'contract':{}},trace,fixture,history)
        self.assertFalse(any(c['dimension']=='answer_facts' for c in checks))


if __name__=='__main__':unittest.main()
