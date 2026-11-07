import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.tasks.task_state import ConversationTask,TaskParameters,TaskPlan,PlannedTask,PlannerParameters
from backend.tasks.task_scheduler import prepare_plan
from backend.tasks.task_execution import read_operations
from backend.tasks.constraint_updates import ConstraintConflict


class CoverageRegressionTests(unittest.TestCase):
    def test_scope_model_timeout_restores_plan_but_total_deadline_does_not(self):
        import backend.agent.langgraph_supervisor_1 as project
        from backend.agent.request_deadline import ModelCallTimeout,RequestDeadlineExceeded
        from evals.fixtures import CaseFixture
        from backend.conversation.chat_runtime import invoke_turn,final_reply
        import backend.observability.chat_events as chat_events
        text='查商品989，再问你们卖电动汽车吗'
        plan=TaskPlan(tasks=[PlannedTask(next='sales',task_type='product_detail',ref='B',
            query='查商品989',parameters=PlannerParameters(product_ids=[989])),
            PlannedTask(next='sales',task_type='conversation',ref='R',source_index=0,
                source_text='你们卖电动汽车吗',query='卖电动汽车吗',
                parameters=PlannerParameters(conversation_intent='out_of_scope'))])
        for error,restored in [(ModelCallTimeout('injected model timeout'),True),
                               (RequestDeadlineExceeded('injected total deadline'),False)]:
            with self.subTest(error=type(error).__name__),CaseFixture('owner') as fixture,\
                    patch.object(project,'invoke_model',side_effect=[plan.model_copy(deep=True),error]) as model,\
                    patch('backend.observability.chat_events.emit_planner_diagnostic',wraps=chat_events.emit_planner_diagnostic) as diagnostic:
                result=invoke_turn(project.app,text,fixture.thread_id)
            self.assertEqual(model.call_count,2)
            answer=final_reply(result['messages']).content
            self.assertEqual('539.9900' in answer,restored)
            self.assertEqual(any(call.kwargs.get('stage')=='scope_repair_rejected' and
                call.kwargs.get('error_type')=='ModelCallTimeout' for call in diagnostic.call_args_list),restored)

    def test_coverage_budget_becomes_list_for_both_planner_operation_choices(self):
        old=ConversationTask(kind='catalog_query',agent='sales',status='completed',
            parameters=TaskParameters(operation='coverage',requested_item='自行车'))
        for operation in ['list','coverage']:
            with self.subTest(operation=operation):
                plan=TaskPlan(tasks=[PlannedTask(next='sales',task_type='catalog_query',
                    continuation=True,target_task_id=old.task_id,query='只查500以内的',
                    parameters=PlannerParameters(operation=operation))])
                tasks,ids,_=prepare_plan(plan,{'tasks':[old.model_dump()]},'只查500以内的',original_inputs=['只查500以内的'])
                task=next(t for t in tasks if t.task_id in ids)
                self.assertEqual(task.parameters.operation,'list')
                self.assertEqual(task.parameters.max_price,500)
                self.assertEqual(task.parameters.product_category,'bicycles')
                tool,args=read_operations(task)[0]
                self.assertEqual((tool,args['operation'],args['max_price']),('query_products','list',500))
                self.assertEqual(args['product_category'],'bicycles')
        self.assertEqual(old.parameters.operation,'coverage')

    def test_unknown_coverage_cannot_expand_to_whole_catalog_on_budget(self):
        old=ConversationTask(kind='catalog_query',agent='sales',status='completed',
            parameters=TaskParameters(operation='coverage',requested_item='电动汽车'))
        plan=TaskPlan(tasks=[PlannedTask(next='sales',task_type='catalog_query',continuation=True,
            target_task_id=old.task_id,parameters=PlannerParameters(operation='list'))])
        with self.assertRaises(ConstraintConflict):
            prepare_plan(plan,{'tasks':[old.model_dump()]},'500以内',original_inputs=['500以内'])

    def test_invalid_scope_repair_preserves_original_business_execution(self):
        import backend.agent.langgraph_supervisor_1 as project
        from evals.fixtures import CaseFixture
        from backend.conversation.chat_runtime import invoke_turn,final_reply
        import backend.observability.chat_events as chat_events
        text='查商品989，再问你们卖电动汽车吗'
        business=PlannedTask(next='sales',task_type='product_detail',ref='B',query='查商品989',
            parameters=PlannerParameters(product_ids=[989]))
        refusal=PlannedTask(next='sales',task_type='conversation',ref='R',query='卖电动汽车吗',
            source_index=0,source_text='你们卖电动汽车吗',
            parameters=PlannerParameters(conversation_intent='out_of_scope'))
        original=TaskPlan(tasks=[business,refusal])
        changed=business.model_copy(deep=True);changed.parameters.product_ids=[776]
        replacement=PlannedTask(next='sales',task_type='catalog_query',ref='R',query='卖电动汽车吗',
            source_index=0,source_text='你们卖电动汽车吗',
            parameters=PlannerParameters(operation='coverage',requested_item='电动汽车'))
        invalid=TaskPlan(tasks=[changed,replacement])
        with CaseFixture('owner') as fixture,patch.object(project,'invoke_model',side_effect=[original,invalid]) as model,\
                patch('backend.observability.chat_events.emit_planner_diagnostic',wraps=chat_events.emit_planner_diagnostic) as diagnostic:
            result=invoke_turn(project.app,text,fixture.thread_id)
        self.assertEqual(model.call_count,2)
        answer=final_reply(result['messages']).content
        self.assertIn('539.9900',answer)
        self.assertIn('不能处理无关请求',answer)
        details=[task for task in result['tasks'] if task['kind']=='product_detail']
        self.assertEqual(details[0]['parameters']['product_ids'],[989])
        self.assertTrue(any(call.kwargs.get('stage')=='scope_repair_rejected' and
            call.kwargs.get('restored_original_plan') for call in diagnostic.call_args_list))
