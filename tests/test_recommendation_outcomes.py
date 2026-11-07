"""Empty retrieval, invalid selection, failed tools and verified empty catalog are different facts."""
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage,HumanMessage,ToolMessage,SystemMessage
from backend.agent.answer_contract import AnswerPlan,render_business_result
from backend.tasks.task_result import TaskResult,ToolEvidence
from backend.tasks.task_state import (ConversationTask,TaskParameters,TaskPlan,PlannedTask,
                        authoritative_product_count,turn_user_inputs)
from backend.tasks.task_scheduler import prepare_plan


class RecommendationOutcomeTests(unittest.TestCase):
    def select(self,rows,draft='{"kind":"data","product_ids":[]}'):
        from backend.agent.langgraph_supervisor_1 import limit_product_plan
        task=ConversationTask(kind='product_search',parameters=TaskParameters(max_price=500,product_category='bicycles',limit=1))
        result=TaskResult(current=rows,product_category='bicycles')
        plan,ids=limit_product_plan(task,result,draft)
        return result,plan,ids

    def test_verified_empty_scope_returns_budget_and_object_not_suitability_fallback(self):
        result,plan,ids=self.select([ToolEvidence(tool='search_products',data={'success':True,'products':[]})])
        self.assertEqual(json.loads(plan)['recommendation_status'],'no_candidates')
        result.catalog_scope={'matched_count':0,'min_price':None,'max_price':500,'product_category':'bicycles'}
        text,audit=render_business_result(result,plan,'sales')
        self.assertIn('没有符合',text);self.assertIn('500',text);self.assertIn('自行车整车',text)
        self.assertNotIn('已检索到候选',text);self.assertNotIn('库存不足',text)
        self.assertEqual(audit['mode'],'verified_empty_catalog')

    def test_empty_semantic_search_cannot_claim_catalog_is_empty(self):
        result,plan,_=self.select([ToolEvidence(tool='search_products',data={'success':True,'products':[]})])
        result.catalog_scope={'matched_count':40,'max_price':1000,'product_category':'bicycles'}
        text,audit=render_business_result(result,plan,'sales')
        self.assertIn('仍有商品',text);self.assertEqual(audit['mode'],'empty_retrieval')

    def test_unconfirmed_catalog_read_never_invents_zero_matches(self):
        result,plan,_=self.select([ToolEvidence(tool='search_products',data={'success':True,'products':[]})])
        text,_=render_business_result(result,plan,'sales')
        self.assertIn('尚未核实',text);self.assertNotIn('没有符合当前价格和类别条件',text)

    def test_failed_search_is_not_empty_catalog(self):
        result,plan,_=self.select([ToolEvidence(tool='search_products',data={'success':False,'error_code':'DATABASE_TIMEOUT'})])
        text,audit=render_business_result(result,plan,'sales')
        self.assertEqual(json.loads(plan)['recommendation_status'],'retrieval_failed')
        self.assertEqual(audit['mode'],'verified_error');self.assertIn('未成功',text)
        self.assertNotIn('没有符合',text)

    def test_partial_success_with_failed_search_is_not_verified_empty(self):
        _,plan,_=self.select([ToolEvidence(tool='search_products',data={'success':True,'products':[]}),
                             ToolEvidence(tool='search_products',data={'success':False})])
        self.assertEqual(json.loads(plan)['recommendation_status'],'retrieval_failed')

    def test_valid_candidates_but_explicit_abstention_remains_insufficient_evidence(self):
        result,plan,_=self.select([ToolEvidence(tool='search_products',data={'success':True,'products':[
            {'product_id':1,'list_price':100,'name':'Bike','product_subcategory_id':1}]})],
            '{"kind":"no_recommendation","recommendation_status":"no_candidates"}')
        self.assertEqual(json.loads(plan)['recommendation_status'],'insufficient_evidence')
        text,_=render_business_result(result,plan,'sales');self.assertIn('未能根据描述确认',text)

    def test_malformed_model_selection_has_its_own_reason(self):
        result,plan,_=self.select([ToolEvidence(tool='search_products',data={'success':True,'products':[
            {'product_id':1,'list_price':100,'name':'Bike','product_subcategory_id':1}]})],'broken')
        self.assertEqual(json.loads(plan)['recommendation_status'],'invalid_selection')
        text,_=render_business_result(result,plan,'sales');self.assertIn('选择清单未通过校验',text)

    def test_verified_empty_seed_skips_unnecessary_specialist_and_tools(self):
        import backend.agent.langgraph_supervisor_1 as project
        task=ConversationTask(kind='product_search',parameters=TaskParameters(limit=1,max_price=500,product_category='bicycles'))
        call='seed'
        seed=[AIMessage(content='',tool_calls=[{'name':'search_products','args':{},'id':call}]),
              ToolMessage(name='search_products',tool_call_id=call,content=json.dumps({'success':True,'products':[]}))]
        scope={'matched_count':0,'min_price':None,'max_price':500,'product_category':'bicycles'}
        with patch.object(project,'seed_product_search',return_value=seed), \
                patch.object(project.order_service,'catalog_scope_summary',return_value=scope), \
                patch.object(project,'sales_agent') as expert:
            value=project._run_specialist_agent(expert,{'task':task.model_dump(),'messages':[HumanMessage(content='budget')],
                'identity_version':1,'identity_customer_id':None},'sales',{'configurable':{'thread_id':'test-empty-'+uuid4().hex}},'sales')
        expert.invoke.assert_not_called();self.assertIsNone(value['execution_error'])
        self.assertEqual(value['execution_catalog_scope']['summary'],scope)
        self.assertEqual(json.loads(value['pending_messages'][-1].content)['recommendation_status'],'no_candidates')

    def test_no_seed_shortcut_for_failed_retrieval_nonempty_scope_or_failed_verification(self):
        import backend.agent.langgraph_supervisor_1 as project
        task=ConversationTask(kind='product_search')
        empty=[ToolMessage(name='search_products',tool_call_id='c',content=json.dumps({'success':True,'products':[]}))]
        error=[ToolMessage(name='search_products',tool_call_id='c',content=json.dumps({'success':False,'error_code':'DATABASE_TIMEOUT'}))]
        with patch.object(project.order_service,'catalog_scope_summary',return_value={'matched_count':3}) as scope:
            self.assertIsNone(project.verify_empty_seed(task,error,{}));scope.assert_not_called()
            self.assertIsNone(project.verify_empty_seed(task,empty,{}))
        with patch.object(project.order_service,'catalog_scope_summary',side_effect=TimeoutError):
            self.assertIsNone(project.verify_empty_seed(task,empty,{}))


class OriginalCountTests(unittest.TestCase):
    def plan(self,count=5):
        return TaskPlan(tasks=[PlannedTask(next='sales',task_type='product_search',
            query=f'推荐{count}款自行车',parameters=TaskParameters(limit=count,product_category='bicycles'))])

    def test_original_user_count_overrides_both_model_fields_and_rewritten_query(self):
        from backend.agent.langgraph_supervisor_1 import validate_planner_counts
        original=['推荐一款自行车。','预算500以内。'];plan=self.plan()
        validate_planner_counts(plan,original)
        tasks,_,_=prepare_plan(plan,{},original[-1],original_inputs=original)
        self.assertEqual(plan.tasks[0].parameters.limit,1);self.assertEqual(tasks[-1].parameters.limit,1)

    def test_latest_explicit_user_count_can_change_the_saved_one(self):
        original=['推荐一款自行车','改成两款，预算1000以内']
        plan=self.plan();self.assertEqual(authoritative_product_count(original,plan.tasks,plan.tasks[0]),2)

    def test_purchase_quantity_and_budget_do_not_change_presentation_count(self):
        plan=self.plan();original=['推荐一款自行车','预算500，每款买两件']
        self.assertEqual(authoritative_product_count(original,plan.tasks,plan.tasks[0]),1)

    def test_multiple_product_scopes_do_not_share_one_count(self):
        plan=self.plan();plan.tasks.append(PlannedTask(ref='T2',next='sales',task_type='product_search',query='推荐两款头盔'))
        self.assertIsNone(authoritative_product_count(['推荐一款自行车，再推荐两款头盔'],plan.tasks,plan.tasks[0]))

    def test_budget_only_followup_preserves_count_from_existing_task(self):
        old=ConversationTask(kind='product_search',parameters=TaskParameters(limit=1,product_category='bicycles'))
        plan=self.plan();plan.tasks[0].continuation=True;plan.tasks[0].target_task_id=old.task_id
        tasks,_,_=prepare_plan(plan,{'tasks':[old.model_dump()]},'预算500以内',original_inputs=['预算500以内'])
        self.assertEqual(tasks[-1].parameters.limit,1)

    def test_current_turn_only_uses_real_user_input_ids(self):
        rows=[HumanMessage(content='推荐五款',id='old'),HumanMessage(content='推荐一款',id='one'),
              AIMessage(content='展示八款'),HumanMessage(content='预算500以内',id='two')]
        self.assertEqual(turn_user_inputs(rows,['one','two']),['推荐一款','预算500以内'])
        self.assertEqual(turn_user_inputs(rows),['预算500以内'])


class CatalogScopeDatabaseTests(unittest.TestCase):
    def test_exact_scope_has_no_retrieval_cutoff_or_keyword(self):
        from backend.orders.adventureworks_service import AdventureWorksService
        from backend.postgres_config import connect_database
        with connect_database() as c:
            expected=c.execute('SELECT count(*) AS n FROM product_catalog WHERE product_subcategory_id IN (1,2,3) AND list_price>0 AND list_price<=500').fetchone()['n']
        actual=AdventureWorksService().catalog_scope_summary(max_price=500,product_category='bicycles')
        self.assertEqual(actual['matched_count'],expected)


class InflightEvidenceTests(unittest.TestCase):
    def setup_evidence(self):
        task=ConversationTask(kind='product_search',parameters=TaskParameters(limit=1,max_price=1000,product_category='bicycles'),
            evidence_call_ids=['earlier-segment','same-segment'])
        def pair(cid,pid,price):
            return [AIMessage(content='',tool_calls=[{'name':'search_products','args':{},'id':cid}]),
                ToolMessage(name='search_products',tool_call_id=cid,content=json.dumps({'success':True,'products':[
                    {'product_id':pid,'name':'Bike','product_model_id':pid,'list_price':price,'product_subcategory_id':1}]}))]
        messages=[HumanMessage(content='old',id='old'),*pair('earlier-segment',99,600),
                  HumanMessage(content='推荐一款自行车',id='first'),*pair('same-segment',1,700),
                  *pair('other-task',88,600),HumanMessage(content='预算1000以内',id='supplement')]
        state={'messages':messages,'inflight_rounds':1,'inflight_user_ids':['first','supplement']}
        return task,state

    def test_only_this_task_and_current_segment_are_reusable(self):
        from backend.conversation.turn_inbox import inflight_evidence_ids
        task,state=self.setup_evidence()
        self.assertEqual(inflight_evidence_ids(state,task),{'same-segment'})
        state['inflight_user_ids']=['not-present'];self.assertEqual(inflight_evidence_ids(state,task),set())

    def test_reused_candidate_is_visible_and_can_be_selected_without_auto_filling(self):
        from backend.agent.context_budget import expert_context
        from backend.conversation.turn_inbox import inflight_evidence_ids
        from backend.tasks.task_result import collect_task_result
        from backend.agent.langgraph_supervisor_1 import limit_product_plan
        task,state=self.setup_evidence();ids=inflight_evidence_ids(state,task)
        _,stats=expert_context([SystemMessage(content='select')],state['messages'],task,reuse_call_ids=ids)
        self.assertEqual(stats['visible_candidate_ids'],[1])
        result=collect_task_result(state['messages'],task)
        result.retained=[e for e in result.history if e.call_id in ids]
        plan,selected=limit_product_plan(task,result,'{"product_ids":[1]}')
        self.assertEqual(selected,[1])
        _,selected=limit_product_plan(task,result,'{"product_ids":[]}');self.assertEqual(selected,[])

    def test_updated_budget_still_filters_same_segment_evidence(self):
        from backend.agent.context_budget import expert_context
        from backend.conversation.turn_inbox import inflight_evidence_ids
        task,state=self.setup_evidence();task.parameters.max_price=500
        _,stats=expert_context([SystemMessage(content='select')],state['messages'],task,
                               reuse_call_ids=inflight_evidence_ids(state,task))
        self.assertEqual(stats['visible_candidate_ids'],[])
