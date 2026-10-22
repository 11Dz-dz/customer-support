"""统一任务、完成结果、专家回退及真实检查点回归；不调用 DeepSeek。"""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

from answer_contract import build_business_answer
from task_execution import execute_read_task, read_operations, seed_product_search
from task_result import collect_task_result
from task_state import ConversationTask, RouteDecision, TaskParameters, complete_task, prepare_task
from tool_execution import TurnPolicy, guarded_tool_call, tool_execution_scope


class TaskStateTest(unittest.TestCase):
    def test_business_identifiers_and_filter_vocabulary_are_normalized_once(self):
        self.assertEqual(TaskParameters(order_id='so43659', color='红色').order_id, 43659)
        self.assertEqual(TaskParameters(color='红色').color, 'Red')
        self.assertEqual(TaskParameters(color='BLUE').color, 'Blue')
        self.assertEqual(TaskParameters(order_id=43659).order_id, 43659)
        for invalid in ['43659 OR 1=1', 'SO999999999999999999', True]:
            with self.assertRaises(ValueError):
                TaskParameters(order_id=invalid)

    def test_task_controls_owner_even_if_router_agent_is_wrong(self):
        task = prepare_task(RouteDecision(next='billing', task_type='quote',
                                         parameters=TaskParameters(product_ids=[915], quantity=2)))
        self.assertEqual(task.agent, 'sales')
        self.assertEqual(task.status, 'ready')

    def test_followup_merges_only_same_task_and_changes_quantity(self):
        old = ConversationTask(kind='quote', agent='sales', parameters=TaskParameters(product_ids=[915], quantity=2))
        new = prepare_task(RouteDecision(next='sales', task_type='quote', continuation=True,
                                        parameters=TaskParameters(quantity=3)), old.model_dump())
        self.assertEqual(new.parameters.product_ids, [915])
        self.assertEqual(new.parameters.quantity, 3)
        self.assertEqual(new.task_id, old.task_id)
        self.assertEqual(new.revision, 2)

    def test_new_task_does_not_inherit_filters_or_order(self):
        old = ConversationTask(kind='order_query', agent='billing', parameters=TaskParameters(order_id=43659))
        new = prepare_task(RouteDecision(next='billing', task_type='order_query'), old.model_dump())
        self.assertIsNone(new.parameters.order_id)
        self.assertEqual(new.status, 'awaiting_input')
        self.assertNotEqual(old.task_id, new.task_id)

    def test_clear_constraint_and_switch_tasks(self):
        old = ConversationTask(kind='catalog_query', parameters=TaskParameters(max_price=100, color='Red'))
        new = prepare_task(RouteDecision(next='sales', task_type='catalog_query', continuation=True,
                                        parameters=TaskParameters(clear_fields=['max_price'])), old.model_dump())
        self.assertIsNone(new.parameters.max_price)
        self.assertEqual(new.parameters.color, 'Red')
        other = prepare_task(RouteDecision(next='billing', task_type='order_query', continuation=True,
                                          parameters=TaskParameters(order_id=43659)), old.model_dump())
        self.assertIsNone(other.parameters.color)

    def test_ambiguous_candidates_are_not_silently_selected(self):
        task = ConversationTask(kind='product_search', agent='sales')
        rows = [HumanMessage(content='推荐商品'), ToolMessage(name='search_products', tool_call_id='r',
                content=json.dumps({'success': True, 'products': [{'product_id': 1}, {'product_id': 2}]}))]
        task = complete_task(task, collect_task_result(rows, task), [1, 2])
        self.assertEqual(task.candidate_product_ids, [1, 2])
        self.assertEqual(task.parameters.product_ids, [])
        quote = prepare_task(RouteDecision(next='sales', task_type='quote', continuation=True,
                                          parameters=TaskParameters(quantity=2)), task.model_dump())
        self.assertEqual(quote.awaiting_fields, ['product_ids'])

    def test_cancelled_task_cannot_reuse_previous_parameters(self):
        old = ConversationTask(kind='quote', status='cancelled', parameters=TaskParameters(product_ids=[915], quantity=2))
        new = prepare_task(RouteDecision(next='sales', task_type='quote', continuation=True), old.model_dump())
        self.assertEqual(new.parameters.product_ids, [])
        self.assertEqual(new.status, 'awaiting_input')


class TaskExecutionTest(unittest.TestCase):
    def test_open_search_obtains_evidence_before_model_and_keeps_budget(self):
        calls = []
        @tool('search_products')
        def search(query: str, min_price: float | None = None, max_price: float | None = None, limit: int = 5) -> dict:
            """Fake retrieval, independent of any user sentence."""
            calls.append((query, max_price))
            return {'success': True, 'products': [{'product_id': 923, 'name': 'Tube', 'list_price': '4.99'}]}
        task = ConversationTask(kind='product_search', parameters=TaskParameters(keyword='tube', max_price=5))
        with tool_execution_scope(TurnPolicy(task=task)):
            delta = seed_product_search(task, 'find a touring tube', {'search_products': search}, {})
        self.assertEqual(calls, [('find a touring tube', 5)])
        self.assertEqual(len(delta), 2)
        self.assertEqual(delta[-1].type, 'tool')

    def test_requested_product_facts_determine_read_operations(self):
        task = ConversationTask(kind='product_detail', parameters=TaskParameters(product_ids=[776]), focus=['promotion'])
        self.assertEqual([name for name, _ in read_operations(task)], ['get_product_info', 'check_promotion'])

    def test_text_tool_result_is_still_evidence(self):
        result = collect_task_result([HumanMessage(content='错误码'),
            ToolMessage(name='search_error_code', tool_call_id='error-1', content='服务器内部错误')])
        self.assertEqual(result.current[0].call_id, 'error-1')
        self.assertTrue(result.current[0].data['success'])

    def test_catalog_task_has_one_execution_and_preserves_empty_result(self):
        calls = []
        @tool('query_products')
        def lookup(max_price: float) -> dict:
            """Fake full catalog query."""
            calls.append(max_price)
            return {'success': True, 'matched_count': 0, 'products': [], 'filters': {'max_price': max_price},
                    'operation': 'list', 'query_scope': 'all_matching_catalog_products'}
        task = ConversationTask(kind='catalog_query', parameters=TaskParameters(max_price=1))
        with tool_execution_scope(TurnPolicy(task=task)):
            delta = execute_read_task(task, {'query_products': lookup}, {})
        result = collect_task_result([HumanMessage(content='一元以内有哪些商品'), *delta], task)
        self.assertEqual(calls, [1])
        self.assertEqual(result.status, 'empty')

    def test_multiple_independent_quote_results_are_all_rendered(self):
        rows = [HumanMessage(content='分别计算两个商品')]
        for pid, name, price in [(1, '商品A', '10'), (2, '商品B', '20')]:
            rows.append(ToolMessage(name='calculate_price', tool_call_id=str(pid), content=json.dumps({
                'success': True, 'product_id': pid, 'product_name': name, 'quantity': 2,
                'unit_price': price, 'subtotal': str(int(price)*2)})))
        text, _ = build_business_answer(rows, '{"focus":["quote"]}', 'sales')
        self.assertIn('商品A', text)
        self.assertIn('商品B', text)
        self.assertIn('20', text)
        self.assertIn('40', text)

    def test_policy_consultation_cannot_execute_refund_even_if_agent_requests_it(self):
        writes = []
        @tool('request_refund')
        def refund(order_id: str) -> dict:
            """Must never execute in a policy inquiry."""
            writes.append(order_id)
            return {'success': True}
        call = {'name': 'request_refund', 'args': {'order_id': '1'}, 'id': 'write'}
        req = SimpleNamespace(tool_call=call, tool=refund, runtime=SimpleNamespace(config={}))
        with tool_execution_scope(TurnPolicy(task=ConversationTask(kind='refund_policy', agent='billing'))):
            result = guarded_tool_call(req, lambda r: refund.invoke(r.tool_call['args']))
        self.assertEqual(writes, [])
        self.assertEqual(json.loads(result.content)['error_code'], 'TASK_TOOL_NOT_ALLOWED')


class TaskGraphTest(unittest.TestCase):
    def test_empty_expert_reply_still_renders_retrieved_evidence(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from evals.fixtures import CaseFixture
        from langchain_core.messages import AIMessage
        class EmptyExpert:
            def invoke(self, state, config):
                return {'messages': state['messages'] + [AIMessage(content='')]}
        with CaseFixture('guest') as fixture, patch.object(project, 'supervisor_router') as router, patch.object(project, 'sales_agent', EmptyExpert()):
            router.invoke.return_value = RouteDecision(next='sales', task_type='product_search', parameters=TaskParameters(keyword='touring tire tube', max_price=5))
            result = invoke_turn(project.app, 'find a touring tube', fixture.thread_id)
            tools = [m for m in result['messages'] if m.type == 'tool']
            self.assertTrue(tools)
            self.assertIn('search_products', [m.name for m in tools])
            self.assertIn('4.9900', result['messages'][-1].content)
            self.assertEqual(result['task_result']['status'], 'completed')

    def test_real_graph_multiturn_task_switch_cancel_and_checkpoint(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from conversation_persistence import get_checkpointer
        from evals.fixtures import CaseFixture
        decisions = [
            RouteDecision(next='sales', task_type='product_detail', parameters=TaskParameters(product_ids=[915])),
            RouteDecision(next='billing', task_type='quote', continuation=True, parameters=TaskParameters(quantity=2)),
            RouteDecision(next='sales', task_type='quote', continuation=True, parameters=TaskParameters(quantity=3)),
            RouteDecision(next='sales', task_type='quote', continuation=True, cancel=True),
            RouteDecision(next='billing', task_type='order_query'),
        ]
        with CaseFixture('owner') as fixture, patch.object(project, 'supervisor_router') as router, \
                patch.object(project, 'sales_agent') as sales, patch.object(project, 'billing_agent') as billing:
            router.invoke.side_effect = decisions
            first = invoke_turn(project.app, '介绍商品915', fixture.thread_id)
            second = invoke_turn(project.app, '买两件', fixture.thread_id)
            third = invoke_turn(project.app, '改成三件', fixture.thread_id)
            cancelled = invoke_turn(project.app, '取消这次报价', fixture.thread_id)
            missing = invoke_turn(project.app, '换个订单查一下', fixture.thread_id)
            sales.invoke.assert_not_called()
            billing.invoke.assert_not_called()
            self.assertIn('商品915', first['messages'][-1].content)
            self.assertEqual(second['task']['parameters']['quantity'], 2)
            self.assertEqual(second['task']['agent'], 'sales')
            self.assertEqual(third['task']['parameters']['quantity'], 3)
            self.assertEqual(second['task']['task_id'], third['task']['task_id'])
            self.assertEqual(cancelled['task']['status'], 'cancelled')
            self.assertEqual(missing['task']['awaiting_fields'], ['order_id'])
            saved = get_checkpointer().get_tuple({'configurable': {'thread_id': fixture.thread_id}})
            self.assertEqual(saved.checkpoint['channel_values']['task']['status'], 'awaiting_input')
            self.assertEqual(missing['pending_messages'], [])

    def test_guest_direct_order_execution_still_enforces_identity(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from evals.fixtures import CaseFixture
        with CaseFixture('guest') as fixture, patch.object(project, 'supervisor_router') as router:
            router.invoke.return_value = RouteDecision(next='billing', task_type='order_query',
                                                       parameters=TaskParameters(order_id=43659))
            result = invoke_turn(project.app, '查询订单43659', fixture.thread_id)
            self.assertEqual(result['task_result']['status'], 'denied')
            self.assertIn('尚未绑定', result['messages'][-1].content)


if __name__ == '__main__':
    unittest.main(verbosity=2)
