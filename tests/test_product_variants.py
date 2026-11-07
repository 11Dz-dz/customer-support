"""Exact model variants, information versus strict budgets, and safe context binding."""
import json
import unittest
from unittest.mock import patch
from langchain_core.messages import AIMessage,ToolMessage
from backend.orders.adventureworks_service import AdventureWorksService
from backend.postgres_config import connect_database
from backend.tasks.task_state import ConversationTask,TaskParameters,PlannedTask,TaskPlan
from backend.tasks.task_scheduler import prepare_plan,resolve_dependencies
from backend.tasks.constraint_updates import ConstraintConflict
from backend.catalog.product_variants import render_variants
from backend.conversation.chat_runtime import invoke_turn,final_reply
from evals.fixtures import CaseFixture


def context(*,displayed=True,ids=(989,)):
    old=ConversationTask(kind='product_search',agent='sales',status='completed' if displayed else 'empty',
        parameters=TaskParameters(product_category='bicycles',keyword='性价比高',limit=1,max_price=564.9899),
        displayed_product_ids=list(ids) if displayed else [],evidence_call_ids=['old'])
    rows=[{'product_id':pid,'name':'Mountain-500','list_price':'539.9900'} for pid in ids]
    messages=[AIMessage(content='',tool_calls=[{'name':'search_products','args':{'query':'性价比高'},'id':'old'}]),
        ToolMessage(name='search_products',tool_call_id='old',content=json.dumps({'success':True,'products':rows,'returned_count':len(rows)})),
        AIMessage(name='sales',content='【销售顾问】\n'+ '\n'.join(f'商品{pid}：已核实的目录商品。' for pid in ids))]
    return old,{'tasks':[old.model_dump()],'messages':messages}


def planned(old,state,text,**parameters):
    item=PlannedTask(next='sales',task_type='product_variants',continuation=True,target_task_id=old.task_id,
        parameters=TaskParameters(**parameters),query=text)
    tasks,ids,_=prepare_plan(TaskPlan(tasks=[item]),state,text,original_inputs=[text])
    return next(t for t in tasks if t.task_id in ids)


class VariantTests(unittest.TestCase):
    def setUp(self):self.service=AdventureWorksService()

    def test_information_returns_silver_with_failed_old_price_condition(self):
        result=self.service.get_variants(989,'color',max_price=564.9899)
        self.assertEqual([p['product_id'] for p in result['products']],[984])
        row=result['products'][0]
        self.assertEqual((row['color'],row['size'],row['product_model_id']),('Silver','40',23))
        self.assertFalse(row['within_price']);self.assertEqual(row['price_difference'],'25.0000')
        text=render_variants(result)
        self.assertIn('商品984',text);self.assertIn('不符合原价格条件',text);self.assertIn('564.9900',text)

    def test_strict_budget_excludes_silver_but_explains_missing_match(self):
        result=self.service.get_variants(989,'color',strict=True,max_price=564.9899)
        self.assertEqual(result['products'],[])
        self.assertEqual(result['excluded_summary']['values'],['Silver'])
        text=render_variants(result)
        self.assertNotIn('商品984',text);self.assertIn('不符合当前价格条件',text)
        self.assertIn('564.9900',text)

    def test_size_lookup_preserves_color_and_reports_real_skus(self):
        result=self.service.get_variants(989,'size')
        self.assertEqual({p['size'] for p in result['products']},{'42','44','48','52'})
        self.assertTrue(all(p['color']=='Black' and p['product_model_id']==23 for p in result['products']))

    def test_requested_value_and_new_other_spec(self):
        result=self.service.get_variants(989,'color',requested_value='Silver',other_value='42',strict=True,max_price=600)
        self.assertEqual([p['product_id'] for p in result['products']],[985])
        self.assertTrue(result['products'][0]['within_price'])

    def test_model_missing_is_unknown_not_no_other_colors(self):
        with connect_database() as c:pid=c.execute('SELECT product_id FROM product_catalog WHERE product_model_id IS NULL LIMIT 1').fetchone()['product_id']
        result=self.service.get_variants(pid,'color')
        self.assertEqual(result['variant_status'],'model_unknown')
        self.assertIn('缺少该商品的型号关联',render_variants(result))

    def test_bind_information_preserves_source_task_and_ignores_model_strict_flag(self):
        old,state=context();before=old.model_dump()
        task=planned(old,state,'那别的颜色呢',product_ids=[760],variant_axis='size',variant_strict=True)
        self.assertEqual((task.parameters.variant_anchor_id,task.parameters.variant_axis,task.parameters.variant_strict),(989,'color',False))
        self.assertEqual(task.parameters.max_price,564.9899)
        self.assertNotEqual(task.task_id,old.task_id);self.assertEqual(state['tasks'][0],before)

    def test_explicit_budget_mode_and_amount_come_from_user(self):
        old,state=context()
        self.assertTrue(planned(old,state,'预算不变，只看其他颜色',variant_strict=False).parameters.variant_strict)
        task=planned(old,state,'其他颜色不超500',max_price=10000)
        self.assertEqual((task.parameters.max_price,task.parameters.variant_strict),(500,True))

    def test_failed_legacy_selection_can_recover_verified_public_reference(self):
        old,state=context(displayed=False)
        task=planned(old,state,'这个商品有没有别的颜色')
        self.assertEqual(task.parameters.variant_anchor_id,989)
        state['messages'][-1]=AIMessage(name='tech_support',content='商品989：不可信的角色来源。')
        self.assertEqual(planned(old,state,'其他颜色').status,'awaiting_input')

    def test_ambiguous_reference_is_not_model_guessed(self):
        old,state=context(ids=(989,760))
        with self.assertRaises(ConstraintConflict):planned(old,state,'其他颜色呢',product_ids=[989])
        self.assertEqual(planned(old,state,'商品989其他颜色').parameters.variant_anchor_id,989)

    def test_explicit_sku_without_history_and_unknown_reference(self):
        item=PlannedTask(next='sales',task_type='product_variants',parameters=TaskParameters(product_ids=[989],variant_axis='color'))
        tasks,_,_=prepare_plan(TaskPlan(tasks=[item]),{},'其他颜色',original_inputs=['其他颜色'])
        self.assertEqual(tasks[-1].status,'awaiting_input')
        tasks,_,_=prepare_plan(TaskPlan(tasks=[item]),{},'商品989其他颜色',original_inputs=['商品989其他颜色'])
        self.assertEqual(tasks[-1].parameters.variant_anchor_id,989)

    def test_relative_variant_price_is_checked_without_model_price(self):
        old,state=context();task=planned(old,state,'比这个更便宜的其他颜色')
        self.assertEqual(task.parameters.variant_relation,'lt');self.assertTrue(task.parameters.variant_strict)
        result=self.service.get_variants(989,'color',strict=True,price_relation='lt')
        self.assertEqual(result['products'],[])

    def test_strict_empty_retains_anchor_for_later_budget_change(self):
        old,state=context();first=planned(old,state,'预算不变，其他颜色')
        first.status='empty';first.parameters.product_ids=[];first.displayed_product_ids=[]
        second=planned(first,{'tasks':[first.model_dump()],'messages':[]},'预算600，其他颜色')
        self.assertEqual((second.parameters.variant_anchor_id,second.parameters.max_price),(989,600))

    def test_graph_displays_outside_price_and_next_quote_uses_displayed_variant(self):
        import backend.agent.langgraph_supervisor_1 as project
        from backend.conversation.session_identity import identity_guard_node
        with CaseFixture('owner') as fixture,patch.object(project,'supervisor_router') as router,patch.object(project.order_service,'search_products',side_effect=AssertionError('No semantic retrieval')):
            old,state=context()
            project.app.update_state({'configurable':{'thread_id':fixture.thread_id}},
                {**state,**identity_guard_node({'messages':[]},{'configurable':{'thread_id':fixture.thread_id}})})
            router.invoke.return_value=TaskPlan(tasks=[PlannedTask(next='sales',task_type='product_variants',continuation=True,target_task_id=old.task_id,parameters=TaskParameters(variant_axis='color'))])
            result=invoke_turn(project.app,'那别的颜色呢',fixture.thread_id);answer=final_reply(result['messages']).content
            self.assertIn('商品984',answer);self.assertIn('不符合原价格条件',answer)
            variant=next(t for t in result['tasks'] if t['task_id'] in result['turn_task_ids'])
            self.assertEqual(variant['displayed_product_ids'],[984]);self.assertEqual(variant['parameters']['product_ids'],[984])
            self.assertEqual(next(t for t in result['tasks'] if t['task_id']==old.task_id),old.model_dump())
            router.invoke.return_value=TaskPlan(tasks=[PlannedTask(next='sales',task_type='quote',continuation=True,target_task_id=variant['task_id'],parameters=TaskParameters(quantity=2))])
            quoted=invoke_turn(project.app,'这个买两件，算标价小计',fixture.thread_id)
            self.assertIn('1129.98',final_reply(quoted['messages']).content)

    def test_tool_guard_rejects_model_widening_strict_variant_query(self):
        from backend.agent.tool_execution import TurnPolicy,tool_execution_scope
        from backend.tasks.task_execution import read_operations
        from tests.test_tool_execution import ToolExecutionTest
        import backend.agent.langgraph_supervisor_1 as project
        old,state=context();task=planned(old,state,'预算不变，查其他颜色')
        name,args=read_operations(task)[0];args['strict']=False
        with tool_execution_scope(TurnPolicy(task=task)),patch.object(project.order_service,'get_variants') as service:
            replies=ToolExecutionTest.run_calls([project.query_product_variants],[(name,args)])
        service.assert_not_called();self.assertEqual(json.loads(replies[0].content)['error_code'],'TASK_TOOL_NOT_ALLOWED')


if __name__=='__main__':unittest.main()
