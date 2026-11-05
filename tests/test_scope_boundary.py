"""Scope refusals are inert public replies; keep business tasks and context."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from chat_runtime import invoke_turn,final_reply
from conversation_replies import REPLIES
from evals.fixtures import CaseFixture
from task_scheduler import prepare_plan
from task_state import TaskPlan,PlannedTask,TaskParameters,ConversationTask


def refusal(**kwargs):
    return PlannedTask(next='sales',task_type='conversation',query='写一个冒泡排序',
        parameters=TaskParameters(conversation_intent='out_of_scope'),**kwargs)


class ScopeBoundaryTests(unittest.TestCase):
    def test_refusal_cannot_inherit_cancel_retry_or_change_conditions(self):
        plan=TaskPlan(tasks=[refusal(continuation=True,cancel=True,retry_failed=True,target_task_id='old',
            depends_on=['old'])],cancel_all=True)
        item=plan.tasks[0]
        self.assertFalse(plan.cancel_all)
        self.assertFalse(item.continuation or item.cancel or item.retry_failed)
        self.assertIsNone(item.target_task_id)
        self.assertEqual(item.depends_on,[])
        self.assertEqual(item.parameters.model_dump(exclude_none=True,exclude_defaults=True),
            {'conversation_intent':'out_of_scope'})

    def test_refusal_preserves_waiting_business_task(self):
        old=ConversationTask(kind='order_query',agent='billing',status='awaiting_input',awaiting_fields=['order_id'])
        tasks,ids,_=prepare_plan(TaskPlan(tasks=[refusal(target_task_id=old.task_id,continuation=True)]),
            {'tasks':[old.model_dump()]},'写一个冒泡排序')
        self.assertEqual(tasks[0].model_dump(),old.model_dump())
        self.assertNotIn(old.task_id,ids)
        self.assertEqual(len(ids),1)

    def test_business_cannot_depend_on_refusal(self):
        with self.assertRaises(ValueError):
            TaskPlan(tasks=[refusal(ref='R'),PlannedTask(next='billing',task_type='order_query',ref='B',depends_on=['R'])])

    def test_refusal_has_no_business_or_expert_execution(self):
        import langgraph_supervisor_1 as project
        with CaseFixture('owner') as f,patch.object(project,'supervisor_router') as router,\
                patch.object(project,'execute_read_task') as direct,patch.object(project.sales_agent,'invoke') as expert:
            router.invoke.return_value=TaskPlan(tasks=[refusal()])
            result=invoke_turn(project.app,'写一个冒泡排序',f.thread_id)
            self.assertEqual(final_reply(result['messages']).content,REPLIES['out_of_scope'])
            direct.assert_not_called();expert.assert_not_called()
            self.assertEqual(result['task']['status'],'completed')
            prompt=router.invoke.call_args.args[0]
            self.assertTrue(any('只处理本客服系统的业务' in m.content for m in prompt))

    def test_mixed_request_keeps_order_amount_and_refuses_code(self):
        import langgraph_supervisor_1 as project
        with CaseFixture('owner') as f,patch.object(project,'supervisor_router') as router:
            router.invoke.return_value=TaskPlan(tasks=[refusal(ref='R'),PlannedTask(next='billing',task_type='order_query',
                ref='B',focus=['amounts'],parameters=TaskParameters(order_id=f.slots['own_order']))])
            result=invoke_turn(project.app,'查订单金额，再写冒泡排序',f.thread_id)
            text=final_reply(result['messages']).content
            self.assertIn(REPLIES['out_of_scope'],text)
            self.assertIn(str(f.slots['own_order']),text)
            self.assertIn('订单应付总额',text)
            self.assertEqual({t['kind'] for t in result['tasks']},{'conversation','order_query'})

    def test_order_number_still_continues_after_irrelevant_turn(self):
        import langgraph_supervisor_1 as project
        with CaseFixture('owner') as f,patch.object(project,'supervisor_router') as router:
            router.invoke.return_value=TaskPlan(tasks=[PlannedTask(next='billing',task_type='order_query')])
            first=invoke_turn(project.app,'查一个订单，我稍后提供编号',f.thread_id)
            old=next(t for t in first['tasks'] if t['kind']=='order_query')
            router.invoke.return_value=TaskPlan(tasks=[refusal()])
            second=invoke_turn(project.app,'写一个冒泡排序',f.thread_id)
            self.assertEqual(next(t for t in second['tasks'] if t['task_id']==old['task_id']),old)
            router.invoke.return_value=TaskPlan(tasks=[PlannedTask(next='billing',task_type='order_query',
                continuation=True,target_task_id=old['task_id'],parameters=TaskParameters(order_id=f.slots['own_order']))])
            third=invoke_turn(project.app,str(f.slots['own_order']),f.thread_id)
            self.assertIn(str(f.slots['own_order']),final_reply(third['messages']).content)
            self.assertEqual(next(t for t in third['tasks'] if t['task_id']==old['task_id'])['status'],'completed')


if __name__=='__main__':unittest.main()
