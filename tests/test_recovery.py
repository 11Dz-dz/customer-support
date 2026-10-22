"""Real PostgreSQL, fake async model I/O; no paid calls or source-order mutations."""
import asyncio
import concurrent.futures
import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from conversation_persistence import get_checkpointer
from postgres_config import connect_database
from request_deadline import (deadline_scope, invoke_model, check_deadline, RequestDeadlineExceeded,
                              ModelCallTimeout, timeout_setting)
from task_evidence import TaskEvidenceRecorder
from task_state import ConversationTask, OperationRecord, TaskPlan, PlannedTask
from human_handoff import (create_handoff, session_handoffs, escalation_reason, ensure_handoff_schema,
                           admin_transition, admin_show)
from tool_result import failure, exception_result


class DeadlineTest(unittest.TestCase):
    def test_native_async_wait_is_cancelled_without_late_result(self):
        cancelled, completed = threading.Event(), threading.Event()
        async def slow(value):
            try:
                await asyncio.sleep(2)
                completed.set()
                return value
            finally:
                cancelled.set()
        started = time.monotonic()
        with deadline_scope(started + .05):
            with self.assertRaises(RequestDeadlineExceeded):
                invoke_model(RunnableLambda(slow), 'ignored')
        self.assertLess(time.monotonic() - started, .8)
        self.assertTrue(cancelled.wait(.5))
        self.assertFalse(completed.is_set())

    def test_model_cap_is_distinct_from_request_exhaustion(self):
        async def slow(value):
            await asyncio.sleep(2)
        with deadline_scope(time.monotonic() + 2):
            with self.assertRaises(ModelCallTimeout):
                invoke_model(RunnableLambda(slow), '', cap=.03)
            check_deadline()

    def test_expired_budget_never_starts_next_model(self):
        called = []
        async def model(value):
            called.append(value)
        with deadline_scope(time.monotonic() - 1):
            with self.assertRaises(RequestDeadlineExceeded):
                invoke_model(RunnableLambda(model), 'no')
        self.assertEqual(called, [])

    def test_scopes_restore_and_bad_setting_is_rejected(self):
        with deadline_scope(time.monotonic() + 1):
            with deadline_scope(time.monotonic() - 1):
                with self.assertRaises(RequestDeadlineExceeded):
                    check_deadline()
            check_deadline()
        for value in ['nan', '0', '301']:
            with patch.dict('os.environ', {'REQUEST_TIMEOUT_SECONDS': value}):
                with self.assertRaises(ValueError):
                    timeout_setting()

    def test_postgres_sleep_uses_remaining_budget(self):
        import psycopg
        started = time.monotonic()
        with deadline_scope(started + .1):
            with self.assertRaises((psycopg.errors.QueryCanceled, RequestDeadlineExceeded)):
                with connect_database() as conn:
                    conn.execute('SELECT pg_sleep(2)')
        self.assertLess(time.monotonic() - started, 1.2)
        with connect_database() as conn:
            self.assertEqual(conn.execute('SELECT 1 AS n').fetchone()['n'], 1)

    def test_expired_transaction_rolls_back_before_commit(self):
        conn = connect_database()
        try:
            conn.execute('CREATE TEMP TABLE deadline_test_items(value integer)')
            conn.commit()
            with deadline_scope(time.monotonic() + .08):
                conn.execute('INSERT INTO deadline_test_items VALUES (1)')
                time.sleep(.1)
                with self.assertRaises(RequestDeadlineExceeded):
                    conn.commit()
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM deadline_test_items').fetchone()['n'], 0)
        finally:
            conn.close()

    def test_snapshot_transaction_is_still_valid(self):
        with deadline_scope(time.monotonic() + 2):
            with connect_database() as conn:
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                self.assertEqual(conn.execute('SELECT 7 AS n').fetchone()['n'], 7)

    def test_recorder_retains_args_and_pairs_once(self):
        recorder = TaskEvidenceRecorder()
        recorder.on_tool_start({'name': 'query_order'}, '{"order_id":"43659"}', run_id='r')
        result = ToolMessage(content=failure('DATABASE_TIMEOUT').model_dump_json(),
                             tool_call_id='c', name='query_order', status='error')
        recorder.on_tool_end(result, run_id='r')
        rows = recorder.recovered()
        self.assertEqual(rows[0].tool_calls[0]['args'], {'order_id': '43659'})
        self.assertEqual(rows[1].tool_call_id, 'c')
        self.assertEqual(len(recorder.recovered(rows)), 2)

    def test_deadline_has_explicit_read_retry_code(self):
        result = exception_result(RequestDeadlineExceeded())
        self.assertEqual(result.error_code, 'REQUEST_DEADLINE')
        self.assertTrue(result.retryable)

    def test_interrupted_write_is_recorded_without_retry_permission(self):
        recorder = TaskEvidenceRecorder()
        recorder.on_tool_start({'name': 'request_refund'}, '{"order_id":"1","reason":"test"}',
                               run_id='r', tool_call_id='write-call')
        recorder.on_tool_error(KeyboardInterrupt(), run_id='r')
        rows = recorder.recovered()
        result = json.loads(rows[1].content)
        self.assertEqual(rows[1].tool_call_id, 'write-call')
        self.assertFalse(result['retryable'])
        self.assertEqual(result['status'], 'error')


class HandoffTest(unittest.TestCase):
    def setUp(self):
        ensure_handoff_schema()
        self.threads = []

    def session(self):
        thread_id = 'test-handoff-' + uuid4().hex
        self.threads.append(thread_id)
        return ({'configurable': {'thread_id': thread_id}},
                {'identity_version': 1, 'identity_customer_id': None, 'messages': []})

    def tearDown(self):
        with connect_database() as conn:
            conn.execute('DELETE FROM customer_service.human_handoffs WHERE thread_id=ANY(%s)', (self.threads,))
        for thread_id in self.threads:
            get_checkpointer().delete_thread(thread_id)

    def test_pending_ticket_reused_and_snapshot_minimized(self):
        config, state = self.session()
        task = ConversationTask(kind='human_handoff', query='PRIVATE_SECRET_MARKER')
        first = create_handoff(config, state, task, 'user_requested')
        second = create_handoff(config, state, ConversationTask(kind='human_handoff'), 'user_requested')
        self.assertEqual(first['ticket_id'], second['ticket_id'])
        record = admin_show(first['ticket_id'])
        self.assertNotIn('PRIVATE_SECRET_MARKER', json.dumps(record, default=str))
        self.assertEqual(len(record['events']), 1)
        self.assertEqual(first['status'], 'pending')

    def test_concurrent_duplicate_requests_create_one_ticket(self):
        config, state = self.session()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda _: create_handoff(config, state,
                ConversationTask(kind='human_handoff'), 'user_requested'), range(4)))
        self.assertEqual(len({r['ticket_id'] for r in rows}), 1)

    def test_admin_claim_and_resolve_are_audited_and_guarded(self):
        config, state = self.session()
        ticket = create_handoff(config, state, ConversationTask(kind='human_handoff'), 'user_requested')
        tid = ticket['ticket_id']
        with self.assertRaises(ValueError):
            admin_transition(tid, 'resolve', 'alice', 'done')
        self.assertEqual(admin_transition(tid, 'claim', 'alice')['status'], 'in_progress')
        admin_transition(tid, 'claim', 'alice')
        with self.assertRaises(ValueError):
            admin_transition(tid, 'claim', 'bob')
        with self.assertRaises(ValueError):
            admin_transition(tid, 'resolve', 'bob', 'not owner')
        with self.assertRaises(ValueError):
            admin_transition(tid, 'resolve', 'alice', '')
        self.assertEqual(admin_transition(tid, 'resolve', 'alice', 'record verified')['status'], 'resolved')
        record = admin_show(tid)
        self.assertEqual([e['event'] for e in record['events']], ['created', 'claim', 'resolve'])
        self.assertEqual(record['ticket']['version'], 3)
        self.assertEqual(session_handoffs(config, state)[0]['status'], 'resolved')

    def test_customer_cannot_see_other_session_tickets_or_internal_notes(self):
        config, state = self.session()
        other_config, other_state = self.session()
        ticket = create_handoff(config, state, ConversationTask(kind='human_handoff'), 'user_requested')
        admin_transition(ticket['ticket_id'], 'claim', 'alice')
        admin_transition(ticket['ticket_id'], 'resolve', 'alice', 'ADMIN_ONLY_MARKER')
        self.assertEqual(session_handoffs(other_config, other_state), [])
        self.assertNotIn('ADMIN_ONLY_MARKER', json.dumps(session_handoffs(config, state), default=str))
        bad_state = dict(state, identity_customer_id=1)
        with self.assertRaises(Exception):
            session_handoffs(config, bad_state)

    def test_resolved_ticket_does_not_block_new_explicit_request(self):
        config, state = self.session()
        first = create_handoff(config, state, ConversationTask(kind='human_handoff'), 'user_requested')
        admin_transition(first['ticket_id'], 'claim', 'alice')
        admin_transition(first['ticket_id'], 'resolve', 'alice', 'done')
        second = create_handoff(config, state, ConversationTask(kind='human_handoff'), 'user_requested')
        self.assertNotEqual(first['ticket_id'], second['ticket_id'])

    def test_only_uncertain_writes_and_persistent_read_failures_escalate(self):
        operation = OperationRecord(operation_id='o', tool='request_refund', arguments={}, status='failed',
                                    call_id='c', error_code='DATABASE_TIMEOUT', outcome='temporary_error')
        task = ConversationTask(kind='refund_request', status='failed', operations=[operation])
        self.assertEqual(escalation_reason(task), 'uncertain_write')
        operation.error_code = 'TURN_DEADLINE'
        self.assertIsNone(escalation_reason(task.model_copy(update={'operations': [operation]})))
        read = operation.model_copy(update={'tool': 'query_order', 'error_code': 'DATABASE_TIMEOUT'})
        self.assertIsNone(escalation_reason(task.model_copy(update={'operations': [read]})))
        read.attempts = 2
        self.assertEqual(escalation_reason(task.model_copy(update={'operations': [read]})), 'persistent_failure')
        for status in ['denied', 'cancelled', 'awaiting_input', 'awaiting_dependency']:
            self.assertIsNone(escalation_reason(task.model_copy(update={'status': status})))
        self.assertIsNone(escalation_reason(ConversationTask(kind='payment_query', status='completed')))

    def test_handoff_survives_new_process(self):
        config, state = self.session()
        ticket = create_handoff(config, state, ConversationTask(kind='human_handoff'), 'user_requested')
        script = """import json,sys
from human_handoff import session_handoffs
print(json.dumps(session_handoffs({'configurable':{'thread_id':sys.argv[1]}},
 {'identity_version':1,'identity_customer_id':None,'messages':[]}),default=str))
"""
        result = subprocess.run([sys.executable, '-c', script, config['configurable']['thread_id']],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True,
                                timeout=25, check=True)
        self.assertEqual(json.loads(result.stdout)[0]['ticket_id'], ticket['ticket_id'])

    def test_blocked_retry_cannot_erase_an_uncertain_submission(self):
        from task_operations import update_operations
        from task_result import ToolEvidence
        args = {'order_id':'1','reason':'test'}
        first = failure('DATABASE_TIMEOUT').model_copy(update={'retryable':False})
        blocked = failure('WRITE_RETRY_NOT_ALLOWED')
        rows = [ToolEvidence(tool='request_refund', call_id=call, arguments=args,
                             data=result.payload(), result=result)
                for call, result in [('first',first),('blocked',blocked)]]
        operations = update_operations([], rows)
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0].error_code, 'WRITE_RETRY_NOT_ALLOWED')
        self.assertTrue(operations[0].requires_review)
        task = ConversationTask(kind='refund_request', status='failed', operations=operations)
        self.assertEqual(escalation_reason(task), 'uncertain_write')
        not_started = update_operations([], [rows[1]])
        self.assertFalse(not_started[0].requires_review)
        self.assertIsNone(escalation_reason(task.model_copy(update={'operations':not_started})))

    def test_commands_work_with_model_unavailable_and_do_not_submit_refund(self):
        import langgraph_supervisor_1 as module
        from chat_runtime import invoke_turn, final_reply
        config, state = self.session()
        thread_id = config['configurable']['thread_id']
        with patch.object(module, 'supervisor_router', SimpleNamespace(invoke=lambda *_: self.fail('model used'))), \
             patch.object(module.order_service, 'request_refund', side_effect=AssertionError('write used')):
            first = invoke_turn(module.app, '/human', thread_id)
            self.assertIn('工单编号：H-', final_reply(first['messages']).content)
            self.assertIn('待人工处理', final_reply(first['messages']).content)
            second = invoke_turn(module.app, '/handoffs', thread_id)
            self.assertIn('待人工处理', final_reply(second['messages']).content)
        self.assertEqual(len(session_handoffs(config, state)), 1)

    def test_ticket_failure_is_not_reported_as_a_success(self):
        import langgraph_supervisor_1 as module
        from chat_runtime import invoke_turn, final_reply
        config, state = self.session()
        with patch.object(module, 'create_handoff', side_effect=ConnectionError('PRIVATE_CONN_MARKER')):
            result = invoke_turn(module.app, '/human', config['configurable']['thread_id'])
        self.assertEqual(result['task']['status'], 'failed')
        self.assertIn('暂未成功确认', final_reply(result['messages']).content)
        self.assertNotIn('PRIVATE_CONN_MARKER', final_reply(result['messages']).content)
        self.assertNotIn('工单编号：H-', final_reply(result['messages']).content)

    def test_tool_result_survives_expert_timeout_and_is_checkpointed(self):
        import langgraph_supervisor_1 as module
        from chat_runtime import invoke_turn, final_reply
        from evals.fixtures import CaseFixture
        from request_deadline import ModelCallTimeout
        with CaseFixture('owner') as fixture:
            self.threads.append(fixture.thread_id)
            plan = TaskPlan(tasks=[PlannedTask(next='billing', task_type='general', query='查询订单')])
            def agent_invoke(inputs, config):
                module.query_order.invoke({'name':'query_order','args':{'order_id':str(fixture.slots['own_order'])},
                    'id':'completed-before-timeout','type':'tool_call'}, config)
                raise ModelCallTimeout()
            with patch.object(module, 'supervisor_router', SimpleNamespace(invoke=lambda *_: plan)), \
                 patch.object(module, 'billing_agent', SimpleNamespace(invoke=agent_invoke)):
                result = invoke_turn(module.app, '查询订单', fixture.thread_id)
            self.assertEqual(result['task']['status'], 'partial')
            self.assertEqual(result['task']['operations'][0]['status'], 'succeeded')
            self.assertEqual(result['task']['failure_code'], 'MODEL_TIMEOUT')
            self.assertIn(str(fixture.slots['own_order']), final_reply(result['messages']).content)
            self.assertTrue(any(isinstance(m, ToolMessage) and m.tool_call_id=='completed-before-timeout'
                                for m in result['messages']))

    def test_uncertain_refund_creates_review_ticket_without_second_submission(self):
        import langgraph_supervisor_1 as module
        from chat_runtime import invoke_turn, final_reply
        from evals.fixtures import CaseFixture
        with CaseFixture('owner') as fixture:
            self.threads.append(fixture.thread_id)
            plan = TaskPlan(tasks=[PlannedTask(next='billing', task_type='refund_request', query='申请退款')])
            def agent_invoke(inputs, config):
                module.request_refund.invoke({'name':'request_refund','args':{
                    'order_id':str(fixture.slots['own_order']), 'reason':'test only'},
                    'id':'uncertain-write','type':'tool_call'}, config)
                raise ModelCallTimeout()
            with patch.object(module, 'supervisor_router', SimpleNamespace(invoke=lambda *_: plan)), \
                 patch.object(module, 'billing_agent', SimpleNamespace(invoke=agent_invoke)), \
                 patch.object(module.order_service, 'request_refund', side_effect=TimeoutError()) as write:
                write.__name__ = 'request_refund'
                result = invoke_turn(module.app, '申请退款', fixture.thread_id)
            self.assertEqual(write.call_count, 1)
            self.assertIn('待人工处理', final_reply(result['messages']).content)
            with connect_database() as conn:
                rows = conn.execute('SELECT reason FROM customer_service.human_handoffs WHERE thread_id=%s',
                                    (fixture.thread_id,)).fetchall()
            self.assertEqual([r['reason'] for r in rows], ['uncertain_write'])

    def test_expired_plan_does_not_start_any_business_operation(self):
        import langgraph_supervisor_1 as module
        from chat_runtime import final_reply
        config, state = self.session()
        config['metadata'] = {'request_deadline_monotonic': time.monotonic() - 1}
        with patch.object(module.order_service, 'get_order', side_effect=AssertionError('late business call')):
            result = module.app.invoke({'messages':[HumanMessage(content='查询订单43659')]}, config)
        self.assertIn('规划等待超时', final_reply(result['messages']).content)
        self.assertEqual(result['turn_task_ids'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
