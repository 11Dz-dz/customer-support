import unittest
from unittest.mock import patch
from catalog_coverage import coverage,lexical_match,render_coverage
from task_state import TaskPlan,PlannedTask,PlannerParameters
from scope_routing import refusal_review,validate_coverage_sources,validate_scope_repair


def refused(text,ref='R',**extra):
    return PlannedTask(next='sales',task_type='conversation',ref=ref,query=text,
        parameters=PlannerParameters(conversation_intent='out_of_scope',scope_reason='other'),**extra)


class CoverageTests(unittest.TestCase):
    def test_english_word_boundaries(self):
        self.assertFalse(lexical_match('car','a carrier for touring'))
        self.assertTrue(lexical_match('car','car, CAR'))

    def test_alias_confirms_chinese_helmet_without_description_hits(self):
        result=coverage('头盔')
        self.assertEqual(result['coverage_status'],'CONFIRMED')
        self.assertEqual(result['confirmed_count'],3)

    def test_confirmed_coverage_survives_the_actual_tool_boundary(self):
        from business_tools import query_products
        from tool_result import normalize_result
        result=normalize_result(query_products.invoke({'operation':'coverage','requested_item':'头盔'}),'query_products')
        self.assertTrue(result.ok)
        self.assertEqual(result.data['coverage_status'],'CONFIRMED')
        self.assertEqual(result.data['returned_count'],0)

    def test_unknown_object_does_not_inherit_bicycle_type(self):
        for item in ['电动自行车','电动汽车','儿童滑板车','car']:
            with self.subTest(item=item):
                result=coverage(item)
                self.assertNotEqual(result['coverage_status'],'CONFIRMED')
                self.assertGreater(result['unclassified_checked'],0)
                text=render_coverage(result)
                self.assertNotIn('本店不卖',text)
                self.assertNotIn('缺货',text)

    def test_database_error_and_empty_item_are_check_failures(self):
        with patch('catalog_coverage.connect_database',side_effect=RuntimeError()):
            result=coverage('头盔')
        self.assertFalse(result['success'])
        self.assertEqual(result['coverage_status'],'CHECK_FAILED')
        self.assertNotIn('未找到',result['tool_message'])
        self.assertEqual(coverage('')['coverage_status'],'CHECK_FAILED')

    def test_reason_and_source_survive_refusal_rebuild(self):
        plan=TaskPlan(tasks=[refused('卖汽车吗',source_index=0,source_text='卖汽车吗')])
        self.assertEqual(plan.tasks[0].parameters.scope_reason,'other')
        self.assertEqual(plan.tasks[0].source_text,'卖汽车吗')
        self.assertTrue(plan._refusal_before_rebuild)

    def test_action_review_does_not_block_single_words(self):
        for text in ['你们卖电动汽车吗','写着多少钱','为什么这款头盔这么贵，价格多少']:
            self.assertTrue(refusal_review(TaskPlan(tasks=[refused(text)]),[text])[0]['eligible'])
        for text in ['写一首关于自行车的诗','写一个查询商品的Python代码','翻译商品描述']:
            self.assertFalse(refusal_review(TaskPlan(tasks=[refused(text)]),[text])[0]['eligible'])

    def test_multi_task_requires_original_fragment(self):
        text='查商品989，再写一首诗'
        plan=TaskPlan(tasks=[PlannedTask(next='sales',task_type='product_detail',ref='B',
            parameters=PlannerParameters(product_ids=[989])),refused('写诗')])
        self.assertEqual(refusal_review(plan,[text])[0]['reason'],'missing_fragment')

    def test_scope_repair_cannot_remove_business_task(self):
        plan=TaskPlan(tasks=[refused('卖汽车吗')])
        wrong=TaskPlan(tasks=[PlannedTask(next='sales',task_type='product_detail',ref='R',
            parameters=PlannerParameters(product_ids=[989]))])
        with self.assertRaises(ValueError):validate_scope_repair(plan,wrong,{'R'})

    def test_coverage_requires_original_object(self):
        plan=TaskPlan(tasks=[PlannedTask(next='sales',task_type='catalog_query',
            parameters=PlannerParameters(operation='coverage',requested_item='头盔'))])
        with self.assertRaises(ValueError):validate_coverage_sources(plan,['卖汽车吗'])

    def test_refusal_repair_uses_shared_allowance(self):
        import langgraph_supervisor_1 as project
        from langchain_core.messages import HumanMessage
        from chat_runtime import turn_config
        from uuid import uuid4
        text='你们卖电动汽车吗'
        first=TaskPlan(tasks=[refused(text)])
        second=TaskPlan(tasks=[PlannedTask(next='sales',task_type='catalog_query',ref='R',query=text,
            source_index=0,source_text=text,
            parameters=PlannerParameters(operation='coverage',requested_item='电动汽车'))])
        with patch.object(project,'invoke_model',side_effect=[first,second]) as model:
            out=project.supervisor_node({'messages':[HumanMessage(content=text)],'tasks':[]},turn_config('scope-'+uuid4().hex))
        self.assertEqual(model.call_count,2)
        self.assertEqual(out['tasks'][0]['parameters']['operation'],'coverage')
