"""Compound requests: real graph/PostgreSQL with a deterministic planner; no model tokens."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from task_state import ConversationTask, PlannedTask, TaskParameters, TaskPlan
from task_scheduler import (PlanConflict, choose_task, prepare_plan, resolve_dependencies, scoped_history)
from tool_execution import RequestBudget, TurnPolicy, tool_execution_scope


def item(kind, agent='sales', ref='T1', **kwargs):
    return PlannedTask(next=agent, task_type=kind, ref=ref, **kwargs)


class PlanTest(unittest.TestCase):
    def test_two_domains_keep_missing_order_task(self):
        tasks, ids, _ = prepare_plan(TaskPlan(tasks=[item('product_search'), item('order_query', 'billing', 'T2')]), {}, '推荐车并查订单')
        self.assertEqual(len(ids), 2)
        self.assertEqual([t.status for t in tasks], ['ready', 'awaiting_input'])

    def test_legacy_checkpoint_migrates(self):
        old = ConversationTask(kind='order_query', agent='billing', status='awaiting_input', awaiting_fields=['order_id'])
        tasks, ids, _ = prepare_plan(TaskPlan(tasks=[item('order_query', 'billing', continuation=True,
            parameters=TaskParameters(order_id=43659))]), {'task': old.model_dump()}, 'SO43659')
        self.assertEqual(ids, [old.task_id])
        self.assertEqual(tasks[0].parameters.order_id, 43659)

    def test_ambiguous_followup_never_guesses_or_executes(self):
        old = [ConversationTask(kind='order_query', agent='billing', status='awaiting_input') for _ in range(2)]
        with self.assertRaises(PlanConflict):
            prepare_plan(TaskPlan(tasks=[item('order_query', 'billing', continuation=True,
                parameters=TaskParameters(order_id=43659))]), {'tasks': [t.model_dump() for t in old]}, 'SO43659')

    def test_cancel_one_does_not_cancel_the_other(self):
        old = [ConversationTask(kind='order_query', agent='billing', status='awaiting_input') for _ in range(2)]
        tasks, ids, _ = prepare_plan(TaskPlan(tasks=[item('order_query', 'billing', cancel=True,
            target_task_id=old[0].task_id)]), {'tasks': [t.model_dump() for t in old]}, '取消第一项')
        self.assertEqual(tasks[0].status, 'cancelled')
        self.assertEqual(tasks[1].status, 'awaiting_input')
        self.assertEqual(ids, [old[0].task_id])

    def test_model_target_cannot_disambiguate_a_bare_order_number(self):
        old = [ConversationTask(kind='order_query', agent='billing', status='awaiting_input', awaiting_fields=['order_id']) for _ in range(2)]
        plan = TaskPlan(tasks=[item('order_query','billing',continuation=True,target_task_id=old[0].task_id,
                                    parameters=TaskParameters(order_id=43659))])
        with self.assertRaises(PlanConflict):
            prepare_plan(plan, {'tasks':[t.model_dump() for t in old]}, 'SO43659')

    def test_cancel_all_preserves_completed_tasks(self):
        old = [ConversationTask(status='completed'), ConversationTask(kind='order_query', status='awaiting_input')]
        tasks, ids, _ = prepare_plan(TaskPlan(tasks=[], cancel_all=True),
                                    {'tasks': [t.model_dump() for t in old]}, '取消全部')
        self.assertEqual([t.status for t in tasks], ['completed', 'cancelled'])
        self.assertEqual(ids, [old[1].task_id])

    def test_cancel_all_can_be_followed_by_a_new_independent_request(self):
        old = ConversationTask(kind='order_query', agent='billing', status='awaiting_input')
        tasks, ids, _ = prepare_plan(TaskPlan(tasks=[item('product_detail',parameters=TaskParameters(product_ids=[915]))],cancel_all=True),
                                    {'tasks':[old.model_dump()]}, '取消全部，然后介绍商品915')
        self.assertEqual([t.status for t in tasks],['cancelled','ready'])
        self.assertEqual(len(ids),2)

    def test_continuation_keeps_only_the_target_tasks_evidence_references(self):
        old = ConversationTask(kind='product_detail',status='completed',evidence_call_ids=['verified-product'],
                               candidate_product_ids=[915],parameters=TaskParameters(product_ids=[915]))
        other = ConversationTask(kind='order_query',agent='billing',status='completed',evidence_call_ids=['private-order'])
        tasks, ids, _ = prepare_plan(TaskPlan(tasks=[item('quote',continuation=True,target_task_id=old.task_id,
            parameters=TaskParameters(quantity=2))]),{'tasks':[old.model_dump(),other.model_dump()]},'买两件')
        continued=next(t for t in tasks if t.task_id==ids[0])
        self.assertEqual(continued.evidence_call_ids,['verified-product'])
        self.assertNotIn('private-order',continued.evidence_call_ids)

    def test_ordinal_reference_uses_display_order_not_sorted_candidate_ids(self):
        old=ConversationTask(kind='order_query',agent='billing',status='completed',candidate_product_ids=[711,776],
                             displayed_product_ids=[776,711],evidence_call_ids=['order'])
        tasks, ids, _=prepare_plan(TaskPlan(tasks=[item('product_detail',continuation=True,target_task_id=old.task_id,
            parameters=TaskParameters(product_position=1,product_ids=[711]))]),{'tasks':[old.model_dump()]},'刚才第一个商品有什么特点')
        selected=next(t for t in tasks if t.task_id==ids[0])
        self.assertEqual(selected.parameters.product_ids,[776])
        self.assertEqual(selected.status,'ready')

    def test_out_of_range_position_requires_clarification(self):
        old=ConversationTask(kind='product_search',status='completed',displayed_product_ids=[915])
        tasks, ids, _=prepare_plan(TaskPlan(tasks=[item('product_detail',continuation=True,target_task_id=old.task_id,
            parameters=TaskParameters(product_position=3))]),{'tasks':[old.model_dump()]},'第三个商品')
        selected=next(t for t in tasks if t.task_id==ids[0])
        self.assertEqual(selected.status,'awaiting_input')
        self.assertEqual(selected.parameters.product_ids,[])

    def test_named_product_detail_without_id_is_a_search(self):
        tasks, _, _=prepare_plan(TaskPlan(tasks=[item('product_detail',parameters=TaskParameters(keyword='Mountain-100'))]),{},'介绍这个系列')
        self.assertEqual(tasks[0].kind,'product_search')

    def test_invalid_planned_order_stays_a_required_field_not_a_router_crash(self):
        plan=TaskPlan.model_validate({'tasks':[{'next':'billing','task_type':'order_query',
                                               'parameters':{'order_id':'invalid OR 1=1'}}]})
        tasks, _, _=prepare_plan(plan,{},'订单编号无效')
        self.assertEqual(tasks[0].status,'awaiting_input')
        self.assertEqual(tasks[0].awaiting_fields,['order_id'])

    def test_order_display_positions_preserve_repeated_line_items(self):
        from task_state import complete_task
        from task_result import collect_task_result
        task=ConversationTask(kind='order_query',agent='billing')
        messages=[HumanMessage(content='订单'),ToolMessage(name='query_order',tool_call_id='q',content=json.dumps({'success':True,
            'items':[{'product_id':776},{'product_id':776},{'product_id':711}]}))]
        completed=complete_task(task,collect_task_result(messages,task))
        self.assertEqual(completed.displayed_product_ids,[776,776,711])

    def test_cycles_and_unknown_dependencies_are_rejected_before_execution(self):
        for plan in [TaskPlan(tasks=[item('product_detail', ref='T1', depends_on=['T2']),
                                    item('order_query', 'billing', 'T2', depends_on=['T1'])]),
                     TaskPlan(tasks=[item('product_detail', depends_on=['UNKNOWN'])])]:
            with self.assertRaises(PlanConflict):
                prepare_plan(plan, {}, '无效依赖')

    def test_deep_dependency_chain_is_rejected(self):
        old=[]
        for i in range(4):
            old.append(ConversationTask(status='completed',depends_on=[old[-1].task_id] if old else []))
        with self.assertRaises(PlanConflict):
            prepare_plan(TaskPlan(tasks=[item('product_detail',depends_on=[old[-1].task_id])]),
                         {'tasks':[t.model_dump() for t in old]},'过深依赖')

    def test_pruning_never_removes_transitive_prerequisites(self):
        root=ConversationTask(status='completed')
        parent=ConversationTask(status='completed',depends_on=[root.task_id])
        child=ConversationTask(kind='product_detail',status='awaiting_dependency',depends_on=[parent.task_id])
        unrelated=[ConversationTask(status='completed') for _ in range(15)]
        tasks, ids, _=prepare_plan(TaskPlan(tasks=[item('product_detail',continuation=True,target_task_id=child.task_id)]),
            {'tasks':[root.model_dump(),parent.model_dump(),*[t.model_dump() for t in unrelated],child.model_dump()]},'继续商品介绍')
        kept={t.task_id for t in tasks}
        self.assertTrue({root.task_id,parent.task_id,child.task_id}.issubset(kept))
        self.assertLessEqual(len(tasks),12)

    def test_dependency_only_uses_authorized_tool_evidence(self):
        parent = ConversationTask(kind='order_query', agent='billing', status='completed', evidence_call_ids=['own'])
        child = ConversationTask(kind='product_detail', status='awaiting_dependency', depends_on=[parent.task_id],
                                 dependency_mode='order_products', parameters=TaskParameters(product_ids=[999]))
        messages = [ToolMessage(name='query_order', tool_call_id='foreign', content=json.dumps({'success': True, 'items':[{'product_id':999}]})),
                    ToolMessage(name='query_order', tool_call_id='own', content=json.dumps({'success':True, 'items':[{'product_id':776}]}))]
        resolved = resolve_dependencies(child, {parent.task_id:parent}, messages)
        self.assertEqual(resolved.parameters.product_ids, [776])
        self.assertEqual(resolved.status, 'ready')

    def test_unselected_recommendations_require_choice_before_quote(self):
        parent = ConversationTask(kind='product_search', status='completed', candidate_product_ids=[915, 916])
        child = ConversationTask(kind='quote', status='awaiting_dependency', depends_on=[parent.task_id],
                                 dependency_mode='selected_product', parameters=TaskParameters(quantity=2))
        resolved = resolve_dependencies(child, {parent.task_id:parent}, [])
        self.assertEqual(resolved.status, 'awaiting_input')
        self.assertEqual(resolved.awaiting_fields, ['product_ids'])

    def test_large_order_is_not_silently_truncated_into_a_complete_description(self):
        parent=ConversationTask(kind='order_query',agent='billing',status='completed',evidence_call_ids=['order'])
        child=ConversationTask(kind='product_detail',status='awaiting_dependency',depends_on=[parent.task_id],dependency_mode='order_products')
        messages=[ToolMessage(name='query_order',tool_call_id='order',content=json.dumps({'success':True,'items':[{'product_id':i} for i in range(30)]}))]
        resolved=resolve_dependencies(child,{parent.task_id:parent},messages)
        self.assertEqual(resolved.status,'awaiting_input')
        self.assertEqual(resolved.awaiting_fields,['product_ids'])
        self.assertEqual(len(resolved.candidate_product_ids),30)

    def test_resume_parent_awakens_waiting_child(self):
        parent = ConversationTask(kind='order_query', agent='billing', status='awaiting_input')
        child = ConversationTask(kind='product_detail', status='awaiting_dependency', depends_on=[parent.task_id], dependency_mode='order_products')
        tasks, ids, _ = prepare_plan(TaskPlan(tasks=[item('order_query', 'billing', continuation=True,
            target_task_id=parent.task_id, parameters=TaskParameters(order_id=43659))]),
            {'tasks': [parent.model_dump(), child.model_dump()]}, 'SO43659')
        self.assertEqual(set(ids), {parent.task_id, child.task_id})

    def test_unmentioned_ready_task_is_not_automatically_replayed(self):
        old = ConversationTask(kind='refund_request', agent='billing')
        new = ConversationTask(kind='product_detail', parameters=TaskParameters(product_ids=[915]))
        _, active = choose_task([old, new], [new.task_id], [], 0)
        self.assertEqual(active.task_id, new.task_id)

    def test_tool_evidence_is_scoped_and_keeps_valid_call_pairs(self):
        task = ConversationTask(query='查我的订单', evidence_call_ids=['bill'])
        messages = [HumanMessage(content='推荐自行车并查订单'),
                    AIMessage(content='', tool_calls=[{'name':'query_order','args':{},'id':'bill'}, {'name':'search_products','args':{},'id':'sales'}]),
                    ToolMessage(name='query_order',tool_call_id='bill',content='{}'),
                    ToolMessage(name='search_products',tool_call_id='sales',content='{}')]
        history = scoped_history(messages, task)
        self.assertEqual(history[0].content, '查我的订单')
        self.assertEqual([c['id'] for c in history[1].tool_calls], ['bill'])
        self.assertEqual([m.tool_call_id for m in history if m.type=='tool'], ['bill'])

    def test_parent_model_budget_survives_node_snapshots(self):
        budget = RequestBudget(max_model_calls=3)
        self.assertFalse(TurnPolicy(parent=budget).next_model())
        budget = RequestBudget(**budget.snapshot())
        self.assertFalse(TurnPolicy(parent=budget).next_model())
        self.assertTrue(TurnPolicy(parent=budget).next_model())

    def test_shared_tool_budget_cannot_reset_between_specialists(self):
        from langchain_core.tools import tool
        from tests.test_tool_execution import ToolExecutionTest
        executions = []
        @tool('get_product_info')
        def lookup(product_name: str) -> dict:
            """Bounded fake product lookup."""
            executions.append(product_name)
            return {'success':True}
        budget = RequestBudget(max_tool_calls=2)
        for number in range(3):
            with tool_execution_scope(TurnPolicy(parent=budget)):
                results = ToolExecutionTest.run_calls([lookup], [('get_product_info', {'product_name':str(number)})])
            budget = RequestBudget(**budget.snapshot())
        self.assertEqual(executions, ['0','1'])
        self.assertEqual(json.loads(results[0].content)['error_code'], 'REQUEST_TOOL_LIMIT')

    def test_requested_recommendation_count_survives_invalid_expert_plan(self):
        import langgraph_supervisor_1 as project
        from task_result import TaskResult, ToolEvidence
        task=ConversationTask(kind='product_search',parameters=TaskParameters(limit=1))
        result=TaskResult(current=[ToolEvidence(tool='search_products',data={'success':True,'products':[
            {'product_id':915,'name':'First'},{'product_id':916,'name':'Second'}]})])
        content,selected=project.limit_product_plan(task,result,'not valid JSON')
        self.assertEqual(selected,[915])
        self.assertEqual(json.loads(content)['product_ids'],[915])


class CompoundGraphTest(unittest.TestCase):
    def test_two_experts_complete_and_stream_only_one_combined_answer(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import stream_turn
        from evals.fixtures import CaseFixture
        with CaseFixture('owner') as f, patch.object(project, 'supervisor_router') as router:
            router.invoke.return_value = TaskPlan(tasks=[item('product_detail', parameters=TaskParameters(product_ids=[915])),
                item('order_query','billing','T2', parameters=TaskParameters(order_id=f.slots['own_order']))])
            parts = list(stream_turn(project.app, '介绍商品915并查我的订单', f.thread_id))
            state = project.app.get_state({'configurable':{'thread_id':f.thread_id}}).values
            text = ''.join(p['text'] for p in parts if p['type']=='text')
            self.assertEqual(text, state['messages'][-1].content)
            self.assertIn('商品915',text)
            self.assertIn(str(f.slots['own_order']),text)
            self.assertEqual([t['status'] for t in state['tasks']], ['completed','completed'])
            self.assertEqual(len([m for m in state['messages'] if m.type=='ai' and not m.tool_calls]),1)
            self.assertEqual(state['turn_budget']['tool_calls'],2)

    def test_missing_order_then_followup_does_not_repeat_product_lookup(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from evals.fixtures import CaseFixture
        with CaseFixture('owner') as f, patch.object(project,'supervisor_router') as router:
            router.invoke.return_value = TaskPlan(tasks=[item('product_detail', parameters=TaskParameters(product_ids=[915])),item('order_query','billing','T2')])
            first = invoke_turn(project.app,'介绍商品915，再查我的订单',f.thread_id)
            self.assertIn('订单号',first['messages'][-1].content)
            waiting = next(t for t in first['tasks'] if t['kind']=='order_query')
            router.invoke.return_value = TaskPlan(tasks=[item('order_query','billing',continuation=True,
                target_task_id=waiting['task_id'],parameters=TaskParameters(order_id=f.slots['own_order']))])
            second = invoke_turn(project.app,str(f.slots['own_order']),f.thread_id)
            tools = [m.name for m in second['messages'][len(first['messages']):] if m.type=='tool']
            self.assertEqual(tools,['query_order'])
            script = "from conversation_persistence import get_checkpointer; import json,sys; s=get_checkpointer().get_tuple({'configurable':{'thread_id':sys.argv[1]}}); print(json.dumps([t['status'] for t in s.checkpoint['channel_values']['tasks']]))"
            saved = subprocess.run([sys.executable,'-c',script,f.thread_id],cwd=Path(__file__).resolve().parent.parent,
                                   capture_output=True,text=True,timeout=30,check=True)
            self.assertEqual(json.loads(saved.stdout),['completed','completed'])

    def test_order_to_product_dependency_uses_actual_authorized_order_items(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from evals.fixtures import CaseFixture
        with CaseFixture('owner') as f, patch.object(project,'supervisor_router') as router:
            router.invoke.return_value = TaskPlan(tasks=[item('order_query','billing',parameters=TaskParameters(order_id=f.slots['own_order'])),
                item('product_detail',ref='T2',depends_on=['T1'],dependency_mode='order_products')])
            result = invoke_turn(project.app,'查订单，再介绍里面的商品',f.thread_id)
            self.assertEqual([m.name for m in result['messages'] if m.type=='tool'],['query_order','get_product_info'])
            self.assertEqual(result['tasks'][-1]['parameters']['product_ids'],[776])
            self.assertIn('商品776',result['messages'][-1].content)

    def test_denied_order_never_passes_product_ids_to_dependent_agent(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from evals.fixtures import CaseFixture
        with CaseFixture('owner') as f, patch.object(project,'supervisor_router') as router:
            router.invoke.return_value = TaskPlan(tasks=[item('order_query','billing',parameters=TaskParameters(order_id=f.slots['foreign_order'])),
                item('product_detail',ref='T2',depends_on=['T1'],dependency_mode='order_products')])
            result = invoke_turn(project.app,'查订单，再介绍里面的商品',f.thread_id)
            self.assertEqual([m.name for m in result['messages'] if m.type=='tool'],['query_order'])
            self.assertEqual(result['tasks'][-1]['status'],'awaiting_dependency')
            self.assertNotIn('商品749',result['messages'][-1].content)

    def test_failed_expert_does_not_discard_independent_order_result(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from evals.fixtures import CaseFixture
        with CaseFixture('owner') as f, patch.object(project,'supervisor_router') as router, patch.object(project,'sales_agent') as sales:
            sales.invoke.side_effect=RuntimeError('PRIVATE_EXCEPTION_BODY')
            router.invoke.return_value=TaskPlan(tasks=[item('general'),item('order_query','billing','T2',parameters=TaskParameters(order_id=f.slots['own_order']))])
            result=invoke_turn(project.app,'销售咨询并查订单',f.thread_id)
            self.assertEqual([t['status'] for t in result['tasks']],['failed','completed'])
            self.assertIn(str(f.slots['own_order']),result['messages'][-1].content)
            self.assertNotIn('PRIVATE_EXCEPTION_BODY',result['messages'][-1].content)

    def test_two_same_domain_tasks_do_not_mix_product_results(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from evals.fixtures import CaseFixture
        with CaseFixture('guest') as f, patch.object(project,'supervisor_router') as router:
            router.invoke.return_value=TaskPlan(tasks=[item('product_detail',parameters=TaskParameters(product_ids=[915])),
                item('product_detail',ref='T2',parameters=TaskParameters(product_ids=[916]))])
            result=invoke_turn(project.app,'分别介绍商品915和916',f.thread_id)
            self.assertIn('商品915',result['turn_outputs'][0]['content'])
            self.assertNotIn('商品916',result['turn_outputs'][0]['content'])
            self.assertIn('商品916',result['turn_outputs'][1]['content'])
            self.assertNotIn('商品915',result['turn_outputs'][1]['content'])

    def test_missing_expert_final_does_not_abort_other_tasks(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from evals.fixtures import CaseFixture
        class NoFinal:
            def invoke(self, state, config):
                return {'messages':state['messages']}
        with CaseFixture('owner') as f, patch.object(project,'supervisor_router') as router, patch.object(project,'sales_agent',NoFinal()):
            router.invoke.return_value=TaskPlan(tasks=[item('general'),item('order_query','billing','T2',parameters=TaskParameters(order_id=f.slots['own_order']))])
            result=invoke_turn(project.app,'商品咨询并查订单',f.thread_id)
            self.assertEqual([t['status'] for t in result['tasks']],['failed','completed'])

    def test_planner_schema_retry_happens_before_any_tools(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from evals.fixtures import CaseFixture
        from langchain_core.exceptions import OutputParserException
        with CaseFixture('guest') as f, patch.object(project,'supervisor_router') as router:
            router.invoke.side_effect=[OutputParserException('invalid JSON'),TaskPlan(tasks=[item('product_detail',parameters=TaskParameters(product_ids=[915]))])]
            result=invoke_turn(project.app,'介绍商品915',f.thread_id)
            self.assertEqual(router.invoke.call_count,2)
            self.assertEqual([m.name for m in result['messages'] if m.type=='tool'],['get_product_info'])

    def test_provider_error_preserves_existing_waiting_tasks_without_tools(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from evals.fixtures import CaseFixture
        with CaseFixture('owner') as f, patch.object(project,'supervisor_router') as router:
            router.invoke.return_value=TaskPlan(tasks=[item('order_query','billing')])
            first=invoke_turn(project.app,'查询订单，编号稍后给',f.thread_id)
            router.invoke.side_effect=RuntimeError('PRIVATE_PROVIDER_BODY')
            second=invoke_turn(project.app,str(f.slots['own_order']),f.thread_id)
            self.assertEqual(second['tasks'],first['tasks'])
            self.assertEqual([m for m in second['messages'][len(first['messages']):] if m.type=='tool'],[])
            self.assertNotIn('PRIVATE_PROVIDER_BODY',second['messages'][-1].content)

    def test_four_tasks_finish_under_graph_recursion_limit(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from evals.fixtures import CaseFixture
        with CaseFixture('guest') as f, patch.object(project,'supervisor_router') as router:
            router.invoke.return_value=TaskPlan(tasks=[item('product_detail',ref=f'T{i}',parameters=TaskParameters(product_ids=[pid])) for i,pid in enumerate([915,916,917,918],1)])
            result=invoke_turn(project.app,'分别查询四件商品',f.thread_id)
            self.assertEqual(result['scheduler_steps'],4)
            self.assertEqual(len(result['turn_outputs']),4)


if __name__=='__main__':
    unittest.main(verbosity=2)
