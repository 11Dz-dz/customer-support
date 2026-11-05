"""Evaluator mutation tests: fluent but wrong results must fail."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import unittest

from evals.current_dataset import current_cases,validate,dataset_hash
from evals.current_contracts import authorized_rows,amount_checks,labeled_amount_checks
from evals.frameworks import trajectory_checks,matching_args,AnswerJudge


class CurrentEvaluationTests(unittest.TestCase):
    def test_current_cases_are_versioned_and_legacy_cases_stay_immutable(self):
        path=Path(__file__).parents[1]/'evals/cases.jsonl';original=path.read_bytes()
        cases=validate(current_cases())
        self.assertEqual(len(cases),80);self.assertEqual(path.read_bytes(),original)
        self.assertTrue(all(c['expected_refunds']==0 for c in cases))
        refunds=[c for c in cases if c['id'] in {'P04','P07','P08','P09'}]
        self.assertTrue(all('no_tools' not in t['contract'] for c in refunds for t in c['turns']))
        changed=deepcopy(cases);changed[0]['turns'][0]['input']='changed'
        self.assertNotEqual(dataset_hash(cases),dataset_hash(changed))

    @staticmethod
    def tool(rows):return [{'name':'query_my_orders','success':True,'result':{'orders':rows},'args':{}}]

    def test_omitted_customer_id_cannot_hide_foreign_order_in_array(self):
        lookup=lambda oid:{'customer_id':1 if oid==123 else 2}
        self.assertTrue(authorized_rows(self.tool([{'order_id':123}]),1,lookup)['passed'])
        bad=authorized_rows(self.tool([{'order_id':123},{'order_id':456}]),1,lookup)
        self.assertFalse(bad['passed']);self.assertTrue(bad['critical'])
        self.assertFalse(authorized_rows(self.tool([{'order_id':123}]),None,lookup)['passed'])

    def test_wrong_tool_amount_cannot_pass_even_with_correct_fluent_answer(self):
        lookup=lambda oid:{'total_due':Decimal('200')}
        self.assertTrue(all(c['passed'] for c in amount_checks(self.tool([{'order_id':123,'total_due':'200'}]),lookup)))
        self.assertFalse(all(c['passed'] for c in amount_checks(self.tool([{'order_id':123,'total_due':'999'}]),lookup)))

    def test_correct_number_under_wrong_label_is_rejected(self):
        ref={'total_due':'200','subtotal':'180','tax_amount':'15','freight':'5'}
        self.assertTrue(all(c['passed'] for c in labeled_amount_checks('商品小计：180；订单应付总额：200。',ref)))
        self.assertFalse(all(c['passed'] for c in labeled_amount_checks('商品小计：200；订单应付总额：180。',ref)))

    def test_sdk_trajectory_checks_accept_alternative_order_and_reject_wrong_filter(self):
        spec={'allowed_tools':['query_my_orders','query_order'],'contract':{'listing':{'filter':'active'}}}
        tools=[{'name':'query_order','args':{'order_id':'123'}},
               {'name':'query_my_orders','args':{'refund_filter':'active','limit':5}}]
        self.assertTrue(all(c['passed'] for c in trajectory_checks({'tools':tools},spec,[])))
        tools.insert(0,{'name':'query_order','args':{'order_id':'456'}})
        self.assertTrue(all(c['passed'] for c in trajectory_checks({'tools':tools},spec,[])))
        tools[-1]['args']['refund_filter']='all'
        self.assertFalse(all(c['passed'] for c in trajectory_checks({'tools':tools},spec,[])))
        tools.append({'name':'request_refund','args':{}})
        checks=trajectory_checks({'tools':tools},spec,[])
        self.assertFalse(checks[0]['passed']);self.assertTrue(checks[0]['critical'])

    def test_argument_normalization_preserves_identity_and_bounds(self):
        self.assertTrue(matching_args({'order_id':'SO123','limit':5},{'order_id':123}))
        self.assertFalse(matching_args({'order_id':'124'},{'order_id':123}))
        self.assertFalse(matching_args({'max_price':501},{'max_price':500}))

    def test_openevals_uses_json_mode_and_preserves_scores_and_usage(self):
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatResult,ChatGeneration
        from langchain_core.runnables import RunnableLambda
        class JsonJudge(BaseChatModel):
            value:bool=True
            @property
            def _llm_type(self):return 'offline-eval-json'
            def _generate(self,messages,stop=None,run_manager=None,**kwargs):
                return ChatResult(generations=[ChatGeneration(message=AIMessage(
                    content=json.dumps({'score':self.value,'reasoning':'offline plumbing check'}),
                    usage_metadata={'input_tokens':10,'output_tokens':5,'total_tokens':15}))])
            def with_structured_output(self,schema,**kwargs):
                if kwargs.get('method')!='json_mode':raise AssertionError('Expected JSON mode')
                return self|RunnableLambda(lambda m:json.loads(m.content))
        for value in [True,False]:
            score=AnswerJudge(JsonJudge(value=value)).score({'input':'test','answer':'test'},[],{}, {})
            self.assertEqual(score['status'],'scored');self.assertIs(score['score'],value)
            self.assertEqual(score['models'][0]['total_tokens'],15)
        malformed=AnswerJudge(JsonJudge().model_copy(update={'value':'not_boolean'})).score(
            {'input':'test','answer':'test'},[],{}, {})
        self.assertEqual(malformed['status'],'unavailable');self.assertIsNone(malformed['score'])
        self.assertIsNotNone(malformed['raw_response'])
        from unittest.mock import patch
        from deepseek_model import ThinkingChatDeepSeek
        from langchain_core.messages import HumanMessage
        model=ThinkingChatDeepSeek(model='deepseek-flash',api_key='offline-not-a-key')
        with patch('langgraph_supervisor_1.create_deepseek_model',return_value=model):
            judge=AnswerJudge()
        payload=judge.model._get_request_payload([HumanMessage(content='offline check')])
        self.assertIs(payload['stream'],False)
        self.assertEqual(payload['extra_body'],__import__('deepseek_model').provider_body('evaluation_judge'))
        self.assertEqual(payload.get('max_tokens',payload.get('max_completion_tokens')),2048)

    def test_judge_error_is_unavailable_not_a_zero_quality_score(self):
        from unittest.mock import patch
        class Broken:
            def model_copy(self,**kwargs):raise TimeoutError('offline timeout')
        score=AnswerJudge(Broken()).score({'input':'test','answer':'test'},[],{}, {})
        self.assertEqual(score['status'],'unavailable');self.assertIsNone(score['score'])

    def test_rescore_keeps_original_report_and_negative_judge_score(self):
        import tempfile
        from evals.current_runner import rescore_trajectory
        original={'run_id':'original','repeat':1,'source_unchanged':True,
            'manifest':{'selected_ids':['C01'],'dataset_sha256':dataset_hash(current_cases())},
            'results':[{'id':'C01','category':'catalog','split':'dev','repeat':1,'auto_pass':False,
                'critical_failure':True,'error_type':None,'unavailable':False,'turns':[
                    {'input':'query','answer':'recorded answer','tools':[{'name':'query_products','args':{}}]*2,
                     'models':[],'judge':{'status':'scored','score':False,'models':[]},
                     'score':{'checks':[{'name':'agentevals_allowed_executions','dimension':'trajectory',
                         'critical':True,'passed':False}],'retrieval':[],'auto_pass':False,'critical_failure':True}}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'original.json';path.write_text(json.dumps(original),encoding='utf-8')
            before=path.read_bytes()
            self.assertEqual(rescore_trajectory(path,Path(tmp)/'out'),1)
            self.assertEqual(path.read_bytes(),before)
            result=json.loads(next((Path(tmp)/'out').glob('*/report.json')).read_text())
            self.assertTrue(result['rule_gate_pass']);self.assertFalse(result['quality_gate_pass'])
            self.assertFalse(result['gate_pass']);self.assertEqual(result['trajectory_rescore']['api_calls'],0)
            self.assertEqual(len(result['trajectory_rescore']['changes']),1)

    @unittest.skipUnless(hasattr(__import__('signal'),'setitimer'),'Linux CLI wall-time guard')
    def test_judge_wall_timeout_interrupts_heartbeat_wait_and_restores_alarm(self):
        import signal
        import time
        from evals.frameworks import judge_deadline,JudgeDeadlineExceeded
        original=signal.getsignal(signal.SIGALRM)
        with self.assertRaises(JudgeDeadlineExceeded):
            with judge_deadline(.01) as enforced:
                self.assertTrue(enforced);time.sleep(.1)
        self.assertEqual(signal.getsignal(signal.SIGALRM),original)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0))

    def test_rejudge_preserves_original_failure_and_never_changes_rule_gate(self):
        from evals.current_runner import rejudge_turn
        from types import SimpleNamespace
        original={'input':'test','answer':'same recorded answer','tools':[],
            'score':{'auto_pass':False,'critical_failure':True},
            'judge':{'status':'scored','score':False,'models':[]}}
        before=deepcopy(original)
        judge=SimpleNamespace(score=lambda *args:{'status':'scored','score':True,'models':[]})
        updated=rejudge_turn(original,{'checks':[],'contract':{}},[],{'total_due':'200'},judge)
        self.assertEqual(original,before)
        self.assertFalse(updated['score']['auto_pass']);self.assertTrue(updated['score']['critical_failure'])
        self.assertFalse(updated['prior_judgements'][0]['score']);self.assertTrue(updated['judge']['score'])

    def test_contract_migration_cannot_replace_questions_or_ignore_manifest(self):
        from evals.current_runner import validate_recorded_contracts
        old=current_cases()
        for case in old:
            if case['id'] in {'P04','P07','P08','P09'}:
                for turn in case['turns']:turn['contract']['no_tools']=True
        with self.assertRaises(ValueError):validate_recorded_contracts(old,dataset_hash(old),False)
        validate_recorded_contracts(old,dataset_hash(old),True)
        with self.assertRaises(ValueError):validate_recorded_contracts(old,'tampered',True)
        old[0]['turns'][0]['input']='Replace a failed question'
        with self.assertRaises(ValueError):validate_recorded_contracts(old,dataset_hash(old),True)


if __name__=='__main__':unittest.main()
