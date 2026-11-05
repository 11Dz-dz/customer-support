"""Candidate selection, budget, lexical ranking, and preserved evidence invariants."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage,HumanMessage,SystemMessage,ToolMessage
from context_budget import expert_context,message_chars
from retrieval_pipeline import candidate_pool,lexical_expression,diverse_selection
from task_state import ConversationTask,TaskParameters


def product(pid,model=None,desc='适用于测试用途',price=10):
    return {'product_id':pid,'product_model_id':pid if model is None else model,'name':'product-'+str(pid),
            'list_price':price,'description_zh':desc,'description_en':'intended use',
            'product_subcategory_id':4,'retrieved_content':'duplicated '*100}


class RetrievalPipelineTests(unittest.TestCase):
    def test_equivalent_terms_are_parameters_not_sql(self):
        score,args=lexical_expression('用途',['长途骑乘','long distance riding'])
        self.assertIn('ILIKE %s',score);self.assertNotIn('长途',score)
        self.assertIn('%长途骑乘%',args);self.assertIn('distance',args)
        _,args=lexical_expression('x',["test' OR 1=1"])
        self.assertIn("%test' OR 1=1%",args)
        with self.assertRaises(ValueError):lexical_expression('x',['x'*81])

    def test_union_deduplicates_calls_and_products_but_preserves_all_families(self):
        a={'success':True,'query':'one','products':[product(1,100),product(2,100),product(3,200)]}
        b={'success':True,'query':'two','products':[product(4,300),product(3,200)]}
        pool=candidate_pool([('search_products',a),('search_products',a),('search_products',b)])
        self.assertEqual(len(pool),4)
        self.assertEqual(len({r['product_model_id'] for r in pool[:3]}),3)

    def test_category_and_budget_rechecked_on_merged_candidates(self):
        task=ConversationTask(kind='product_search',parameters=TaskParameters(max_price=20,product_category='components'))
        rows=[product(1),product(2,price=100),{**product(3),'product_subcategory_id':1}]
        pool=candidate_pool([('search_products',{'success':True,'products':rows})],task)
        self.assertEqual([p['product_id'] for p in pool],[1])

    def test_variant_comparison_is_explicit(self):
        rows=[product(1,100),product(2,100),product(3,200)]
        self.assertEqual(diverse_selection([1,2,3],rows,3),[1,3])
        self.assertEqual(diverse_selection([1,2,3],rows,3,True),[1,2,3])

    def test_second_search_eighth_candidate_survives_prompt_compaction(self):
        task=ConversationTask(kind='product_search',parameters=TaskParameters(limit=5,keyword='purpose',product_category='all'))
        messages=[HumanMessage(content='find purpose')]
        for index,rows in enumerate([[product(i) for i in range(1,6)],[product(i) for i in range(6,16)]]):
            cid='call-'+str(index)
            messages.extend([AIMessage(content='',tool_calls=[{'name':'search_products','args':{'query':str(index)},'id':cid}]),
                ToolMessage(name='search_products',tool_call_id=cid,content=json.dumps({'success':True,'query':str(index),'products':rows}))])
        before=deepcopy(messages)
        output,stats=expert_context([SystemMessage(content='select based on purpose')],messages,task)
        self.assertIn(13,stats['visible_candidate_ids'])
        pool=next(m for m in output if m.content.startswith('【程序提供的本任务统一候选池'))
        self.assertIn('"product_id": 13',pool.content)
        self.assertNotIn('retrieved_content',pool.content)
        self.assertLessEqual(message_chars(output),16000)
        self.assertEqual(messages,before)

    def test_small_budget_reports_omissions_instead_of_silent_first_five(self):
        task=ConversationTask(kind='product_search',parameters=TaskParameters(limit=1))
        rows=[product(i,desc='用途'*250) for i in range(1,21)]
        messages=[HumanMessage(content='purpose'),AIMessage(content='',tool_calls=[{'name':'search_products','args':{},'id':'c'}]),
                  ToolMessage(name='search_products',tool_call_id='c',content=json.dumps({'success':True,'products':rows}))]
        output,stats=expert_context([SystemMessage(content='bounded')],messages,task,max_chars=7000)
        self.assertTrue(stats['omitted_candidate_ids']);self.assertLessEqual(message_chars(output),7000)

    def test_invalid_plan_never_automatically_picks_first_five(self):
        from langgraph_supervisor_1 import limit_product_plan
        from task_result import TaskResult,ToolEvidence
        result=TaskResult(current=[ToolEvidence(tool='search_products',data={'success':True,'products':[product(i) for i in range(1,9)]})])
        task=ConversationTask(kind='product_search',parameters=TaskParameters(limit=5))
        for content in ['invalid','{"product_ids":[]}','{"product_ids":[999]}']:
            plan,selected=limit_product_plan(task,result,content)
            self.assertEqual(selected,[]);self.assertEqual(json.loads(plan)['kind'],'no_recommendation')
        plan,selected=limit_product_plan(task,result,'{"product_ids":[8,7]}')
        self.assertEqual(selected,[8,7])

    def test_changed_purpose_does_not_reuse_old_keyword_expansions(self):
        from task_state import PlannedTask,prepare_task
        old=ConversationTask(kind='product_search',agent='sales',parameters=TaskParameters(keyword='touring',search_terms=['long distance']))
        plan=PlannedTask(next='sales',task_type='product_search',continuation=True,parameters=TaskParameters(keyword='helmet'))
        new=prepare_task(plan,old.model_dump())
        self.assertEqual(new.parameters.search_terms,[])

    def test_empty_new_selection_cannot_reuse_previous_selected_product(self):
        from task_state import complete_task
        from task_result import TaskResult,ToolEvidence
        task=ConversationTask(kind='product_search',parameters=TaskParameters(product_ids=[1]))
        result=TaskResult(current=[ToolEvidence(tool='search_products',data={'success':True,'products':[product(2)]})])
        saved=complete_task(task,result,[])
        self.assertEqual(saved.parameters.product_ids,[]);self.assertEqual(saved.displayed_product_ids,[])

    def test_retry_selection_can_use_verified_retained_candidates(self):
        from langgraph_supervisor_1 import limit_product_plan
        from task_result import TaskResult,ToolEvidence
        result=TaskResult(retained=[ToolEvidence(tool='search_products',data={'success':True,'products':[product(1)]})],
                          current=[ToolEvidence(tool='search_products',data={'success':True,'products':[product(2)]})])
        task=ConversationTask(kind='product_search',retry_failed=True,parameters=TaskParameters(limit=2))
        plan,selected=limit_product_plan(task,result,'{"product_ids":[1,2]}')
        self.assertEqual(selected,[1,2])

    def test_retry_context_keeps_successful_same_task_evidence(self):
        from task_state import OperationRecord
        task=ConversationTask(kind='product_search',retry_failed=True,operations=[OperationRecord(
            operation_id='old',tool='search_products',arguments={},status='succeeded',call_id='old')])
        messages=[HumanMessage(content='original'),AIMessage(content='',tool_calls=[
            {'name':'search_products','args':{},'id':'old'}]),ToolMessage(name='search_products',tool_call_id='old',
            content=json.dumps({'success':True,'products':[product(1)]})),HumanMessage(content='retry')]
        _,stats=expert_context([SystemMessage(content='select')],messages,task)
        self.assertEqual(stats['visible_candidate_ids'],[1])

    def test_successful_search_retry_still_invokes_specialist_for_selection(self):
        import langgraph_supervisor_1 as project
        task=ConversationTask(kind='product_search',retry_failed=True)
        evidence=[AIMessage(content='',tool_calls=[{'name':'search_products','args':{},'id':'retry'}]),
                  ToolMessage(name='search_products',tool_call_id='retry',content=json.dumps(
                      {'success':True,'products':[product(1)]})),AIMessage(content='{"product_ids":[]}')]
        seen=[]
        class Expert:
            def invoke(self,state,config):
                seen.extend(state['messages'])
                return {'messages':state['messages']+[AIMessage(content='{"product_ids":[1]}')]}
        with patch.object(project,'execute_read_task',return_value=evidence):
            result=project._run_specialist_agent(Expert(),{'task':task.model_dump(),
                'identity_version':1,'identity_customer_id':None,
                'messages':[HumanMessage(content='retry')]},'sales',
                {'configurable':{'thread_id':'test-retrieval-'+uuid4().hex}},'sales')
        self.assertIsNone(result['execution_error'])
        self.assertTrue(any(isinstance(m,ToolMessage) for m in seen))
        self.assertFalse(any(isinstance(m,AIMessage) and not m.tool_calls for m in seen))
        self.assertEqual(json.loads(result['pending_messages'][-1].content)['product_ids'],[1])


if __name__=='__main__':unittest.main()
