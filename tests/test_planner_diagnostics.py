import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.tasks.task_state import TaskPlan, PlannedTask, PlannerParameters, presentation_count_matches
from backend.tasks.task_scheduler import prepare_plan
from backend.tasks.constraint_updates import ConstraintConflict, may_repair_proposal


class PlannerDiagnosticsTests(unittest.TestCase):
    def test_count_matches_keep_offsets_values_and_unknown_counts(self):
        for text, value in [('三款', 3), ('3个', 3), ('两件', 2), ('前五', 5),
                            ('top3', 3), ('最多10条', 10), ('几款', None),
                            ('推荐一个头盔', 1)]:
            with self.subTest(text=text):
                matches = presentation_count_matches(text)
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0]['value'], value)
                start, end = matches[0]['span']
                self.assertTrue(text[start:end])

    def test_classifiers_ids_and_time_do_not_prevent_repair(self):
        conflict = ConstraintConflict('limit', 'missing evidence', repairable=True)
        for text in ['换个问题', '这个', '那个商品', '哪个更好', '一个问题',
                     '有个订单', '商品776', '错误码500', '3天前的订单']:
            with self.subTest(text=text):
                self.assertEqual(presentation_count_matches(text), [])
                self.assertTrue(may_repair_proposal(conflict, [text]))

    def test_unknown_quantity_intent_is_not_erased(self):
        self.assertFalse(may_repair_proposal(
            ConstraintConflict('limit', 'unknown count', repairable=True), ['列出几款头盔']))

    def test_detail_with_id_and_keyword_does_not_convert(self):
        rows = []
        plan = TaskPlan(tasks=[PlannedTask(next='sales', task_type='product_detail',
            query='商品776的价格', parameters=PlannerParameters(product_ids=[776], keyword='自行车'))])
        tasks, _, _ = prepare_plan(plan, {}, '商品776的价格', diagnostic=lambda **row: rows.append(row))
        self.assertEqual(tasks[0].kind, 'product_detail')
        self.assertIsNone(rows[0]['conversion_reason'])

    def test_missing_id_conversion_is_visible_before_rejection(self):
        rows = []
        plan = TaskPlan(tasks=[PlannedTask(next='sales', task_type='product_detail',
            query='查商品776', parameters=PlannerParameters(keyword='776', limit=1))])
        with self.assertRaises(ConstraintConflict):
            prepare_plan(plan, {}, '查商品776', original_inputs=['查商品776'],
                         diagnostic=lambda **row: rows.append(row))
        self.assertEqual(rows[0]['after']['kind'], 'product_search')
        self.assertEqual(rows[0]['parameter_origins']['limit'], 'model')
        self.assertIsNotNone(rows[0]['conversion_reason'])

    def test_m09_bad_candidate_gets_one_repair_and_logs_both_plans(self):
        import backend.agent.langgraph_supervisor_1 as project
        from langchain_core.messages import HumanMessage
        from backend.conversation.chat_runtime import turn_config
        query = '现在换个问题，商品776的目录标价是多少？'
        bad = TaskPlan(tasks=[PlannedTask(next='sales', task_type='product_search',
            query=query, parameters=PlannerParameters(limit=1, product_category='all'))])
        good = TaskPlan(tasks=[PlannedTask(next='sales', task_type='product_detail',
            query=query, parameters=PlannerParameters(product_ids=[776]))])
        with patch.object(project, 'invoke_model', side_effect=[bad, good]) as invoke, \
                patch('backend.observability.chat_events.emit_planner_diagnostic') as diagnostic:
            result = project.supervisor_node({'messages': [HumanMessage(content=query)], 'tasks': []},
                                             turn_config('diagnostic-' + uuid4().hex))
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(result['tasks'][0]['kind'], 'product_detail')
        stages = [call.kwargs['stage'] for call in diagnostic.call_args_list]
        self.assertEqual(stages, ['model_candidate_received', 'candidate_rejected', 'repair_decision',
                                  'model_candidate_received', 'candidate_accepted'])
        self.assertTrue(diagnostic.call_args_list[2].kwargs['repair_attempted'])

    def test_private_logs_redact_secrets_without_streaming(self):
        from backend.observability.chat_events import emit_planner_diagnostic
        with patch('backend.observability.chat_events._logger.info') as log, \
                patch('backend.observability.langsmith_tracing.record_trace_event') as trace, \
                patch('langgraph.config.get_stream_writer') as stream:
            emit_planner_diagnostic(stage='candidate_rejected', candidate={'api_key': 'secret'})
        self.assertNotIn('secret', log.call_args.args[0])
        self.assertIn('[REDACTED]', log.call_args.args[0])
        trace.assert_called_once()
        stream.assert_not_called()
