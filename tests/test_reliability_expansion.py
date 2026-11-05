import json
import unittest
from langchain_core.messages import HumanMessage,SystemMessage
from task_state import TaskParameters,PlannedTask,TaskPlan,ConversationTask,PlannerParameters,QuoteLine
from task_scheduler import prepare_plan
from catalog_query import CatalogExpression,CatalogGroup,CatalogPredicate,matches,sql
from constraint_updates import ConstraintConflict,recognized
from context_budget import task_context_message,router_context,message_chars,planner_schema,ContextBudgetError
from task_execution import read_operations


def expression(*conditions):
    return CatalogExpression(groups=[CatalogGroup(conditions=[CatalogPredicate(field=f,operator=op,values=[v]) for f,op,v in conditions])])


def bind(text,kind='catalog_query',previous=None,params=None,update=None):
    item=PlannedTask(next='sales',task_type=kind,continuation=previous is not None,
        target_task_id=previous.task_id if previous else None,query=text,
        parameters=PlannerParameters.model_validate(params or {}),query_update=update)
    tasks,ids,_=prepare_plan(TaskPlan(tasks=[item]),{'tasks':[previous.model_dump()]} if previous else {},text,original_inputs=[text])
    return next(t for t in tasks if t.task_id in ids)


class ReliabilityExpansionTests(unittest.TestCase):
    def test_task_prefix_is_bounded_without_mutating_saved_records(self):
        tasks=[ConversationTask(kind='product_detail',status='completed',query='查商品'+str(i),
            parameters=TaskParameters(product_ids=[i],search_terms=['x'*70]*8),
            parameter_sources={'max_price':{'source_text':'x'*400}}) for i in range(12)]
        before=[t.model_dump() for t in tasks]
        user=HumanMessage(content='查看商品872，然后买两件')
        prefix=[SystemMessage(content='system rules'*700),task_context_message(tasks)]
        result,stats=router_context(prefix,[user],max_chars=13000)
        self.assertLessEqual(message_chars(result),13000);self.assertEqual(result[-1].content,user.content)
        self.assertEqual(before,[t.model_dump() for t in tasks]);self.assertEqual(stats['task_count'],12)
        content=''.join(m.content for m in result)
        self.assertTrue(all(t.task_id in content for t in tasks))

    def test_context_distinguishes_system_overhead_from_large_current_input(self):
        with self.assertRaisesRegex(ContextBudgetError,'系统提示'):
            router_context([SystemMessage(content='x'*2000)],[HumanMessage(content='短问题')],max_chars=1000)
        with self.assertRaisesRegex(ContextBudgetError,'当前问题过长'):
            router_context([], [HumanMessage(content='x'*1000)],max_chars=1000)

    def test_compact_schema_keeps_actual_constraints(self):
        schema=planner_schema(TaskPlan.model_json_schema())
        self.assertNotIn('title',schema)
        self.assertIn('required',schema['$defs']['QuoteLine']);self.assertIn('minimum',schema['$defs']['QuoteLine']['properties']['quantity'])
        self.assertIn('description',planner_schema({'type':'object','properties':{'description':{'type':'string','description':'help'}}})['properties'])

    def test_type_filter_sql_and_candidate_check_agree(self):
        expr=expression(('product_type','eq','helmet'),('price','lte',600))
        clause,args=sql(expr)
        self.assertIn('product_subcategory_id',clause);self.assertEqual(args[0],[31])
        self.assertTrue(matches({'product_subcategory_id':31,'list_price':35},expr))
        self.assertFalse(matches({'product_subcategory_id':37,'list_price':5},expr))

    def test_named_object_is_not_broader_accessory_category(self):
        t=bind('列出三款头盔，按价格从低到高。',params={'keyword':'头盔','limit':3})
        self.assertIsNone(t.parameters.keyword)
        self.assertTrue(matches({'product_subcategory_id':31},t.parameters.catalog_filter))
        self.assertFalse(matches({'product_subcategory_id':37},t.parameters.catalog_filter))

    def test_type_change_keeps_budget(self):
        old=bind('自行车预算600以内',update={'source_index':0,'source_text':'自行车预算600以内','operation':'replace',
            'expression':expression(('category','eq','bicycles'),('price','lte',600))})
        text='不看整车了，改看头盔，预算不变'
        t=bind(text,previous=old,update={'source_index':0,'source_text':text,'expression':expression(('category','eq','accessories'))})
        self.assertTrue(matches({'product_subcategory_id':31,'list_price':35},t.parameters.catalog_filter))
        self.assertFalse(matches({'product_subcategory_id':37,'list_price':5},t.parameters.catalog_filter))
        self.assertEqual(t.parameters.max_price,600)

    def test_negative_type_is_not_injected_as_positive(self):
        text='不要头盔'
        t=bind(text,update={'source_index':0,'source_text':text,'expression':expression(('product_type','not_in','helmet'))})
        self.assertFalse(matches({'product_subcategory_id':31},t.parameters.catalog_filter))
        self.assertTrue(matches({'product_subcategory_id':37},t.parameters.catalog_filter))

    def test_unmentioned_type_is_rejected(self):
        with self.assertRaises(ConstraintConflict):
            bind('预算600以内',update={'source_index':0,'source_text':'预算600以内','expression':expression(('product_type','eq','helmet'))})

    def test_explicit_scope_reset_drops_prior_object_budget_and_keyword(self):
        old=bind('头盔600以内',params={'keyword':'头盔'},update={'source_index':0,'source_text':'头盔600以内','expression':expression(('price','lte',600))})
        text='改查整个目录里最便宜的五条，排除0标价'
        t=bind(text,previous=old,update={'source_index':0,'source_text':text,'operation':'replace'})
        self.assertIsNone(t.parameters.max_price);self.assertIsNone(t.parameters.keyword)
        self.assertTrue(matches({'product_subcategory_id':1,'list_price':3000},t.parameters.catalog_filter))

    def test_nonreset_cannot_drop_saved_budget(self):
        old=ConversationTask(kind='catalog_query',parameters=TaskParameters(max_price=600))
        t=bind('不要黑色',previous=old,update={'source_index':0,'source_text':'不要黑色','operation':'replace','expression':expression(('color','ne','Black'))})
        self.assertEqual(t.parameters.max_price,600)
        self.assertFalse(matches({'color':'Black','list_price':300},t.parameters.catalog_filter))
        self.assertFalse(matches({'color':'Red','list_price':700},t.parameters.catalog_filter))

    def test_count_wording(self):
        for text in ['剩下的记录有多少','符合条件的商品有多少','银色款式多少条']:
            self.assertEqual(recognized(text)[0]['operation'],'count')

    def test_detail_budget_survives_variant_handoff(self):
        old=bind('先看商品989，预算550以内',kind='product_detail',params={'product_ids':[989],'max_price':550})
        old.displayed_product_ids=[989];old.status='completed'
        t=bind('预算不变，只看同款其他颜色',kind='product_variants',previous=old,params={'variant_axis':'color'})
        self.assertEqual(t.parameters.max_price,550);self.assertTrue(t.parameters.variant_strict)

    def test_new_independent_task_does_not_inherit_budget(self):
        old=ConversationTask(kind='product_detail',parameters=TaskParameters(product_ids=[989],max_price=550))
        t=bind('商品984还有什么颜色',kind='product_variants',params={'product_ids':[984],'variant_axis':'color'})
        self.assertIsNone(t.parameters.max_price)

    def test_quote_distinct_line_quantities_reach_tool(self):
        t=bind('商品989两件，再加商品984一件，合并算标价小计',kind='quote',params={'product_ids':[989,984],
            'quote_items':[{'product_id':989,'quantity':2},{'product_id':984,'quantity':1}]})
        self.assertEqual(t.status,'ready')
        items=read_operations(t)[0][1]['bundles'][0]['items']
        self.assertEqual(items,[{'product_id':989,'quantity':2},{'product_id':984,'quantity':1}])

    def test_uniform_line_quantities_cover_each_named_product(self):
        t=bind('商品989和984各买两件',kind='quote',params={'product_ids':[989,984],'quantity':2})
        self.assertEqual([x.product_id for x in t.parameters.quote_items],[989,984])

    def test_incomplete_per_item_quantities_do_not_silently_drop_an_item(self):
        t=bind('商品989两件，商品984也要',kind='quote',params={'product_ids':[989,984]})
        self.assertEqual(t.status,'awaiting_input');self.assertEqual(t.parameters.product_ids,[989,984])
        self.assertEqual(t.parameters.quote_items,[QuoteLine(product_id=989,quantity=2)])
        from task_execution import clarification
        self.assertIn('商品984',clarification(t));self.assertIn('数量2',clarification(t))

    def test_bare_quantity_fills_only_one_missing_quote_line(self):
        old=bind('商品989两件，商品984也要',kind='quote',params={'product_ids':[989,984]})
        t=bind('买三件',kind='quote',previous=old,params={'quantity':3,'quote_items':[{'product_id':989,'quantity':2},{'product_id':984,'quantity':3}]})
        self.assertEqual(t.status,'ready')
        self.assertEqual(t.parameters.quote_items,[QuoteLine(product_id=989,quantity=2),QuoteLine(product_id=984,quantity=3)])

    def test_explicit_missing_sku_quantity_preserves_known_quote_lines(self):
        old=bind('商品989两件，商品984也要',kind='quote',params={'product_ids':[989,984]})
        t=bind('商品984买三件',kind='quote',previous=old,params={'product_ids':[984],'quantity':3})
        self.assertEqual(t.parameters.quote_items,[QuoteLine(product_id=989,quantity=2),QuoteLine(product_id=984,quantity=3)])

    def test_existing_scalar_quote_checkpoint_remains_usable(self):
        old=ConversationTask(kind='quote',status='completed',parameters=TaskParameters(product_ids=[989],quantity=2))
        t=bind('继续按刚才的数量核算',kind='quote',previous=old)
        self.assertEqual(t.status,'ready');self.assertEqual(t.parameters.quote_items,[QuoteLine(product_id=989,quantity=2)])
        self.assertTrue(t.parameter_sources['quote_items']['legacy_scalar_adapted'])

    def test_zero_quantity_is_clear_validation_error(self):
        with self.assertRaisesRegex(ConstraintConflict,'1到10000'):
            bind('商品989买0件给我报价',kind='quote',params={'product_ids':[989],'quantity':0})

    def test_fractional_negative_and_excessive_quantities_are_not_rounded_or_clamped(self):
        for value in ['1.5','-1','10001']:
            with self.subTest(value=value),self.assertRaisesRegex(ConstraintConflict,'1到10000'):
                bind('商品989买'+value+'件',kind='quote',params={'product_ids':[989]})

    def test_model_cannot_invent_a_quote_line_or_quantity(self):
        with self.assertRaises(ConstraintConflict):bind('商品989两件',kind='quote',params={'quote_items':[{'product_id':984,'quantity':3}]})

    def test_quote_does_not_accept_user_or_model_unit_prices(self):
        with self.assertRaises(ValueError):QuoteLine(product_id=989,quantity=2,unit_price=1)
        t=bind('按每件1块钱算商品989两件，可以吗',kind='quote',params={'product_ids':[989],'quantity':2})
        self.assertNotIn('unit_price',str(read_operations(t)))

    def test_explicit_position_units_share_one_resolver(self):
        from product_references import explicit_position
        for unit in ['条','件','款','个','项']:self.assertEqual(explicit_position('找比第二'+unit+'便宜的'),2)
        self.assertEqual(explicit_position('第十二条'),12)

    def test_cancelled_quote_is_not_reactivated_by_line_binding(self):
        old=bind('商品989两件',kind='quote',params={'product_ids':[989],'quantity':2})
        old.status='completed'
        plan=TaskPlan(tasks=[PlannedTask(next='sales',task_type='quote',target_task_id=old.task_id,cancel=True)])
        tasks,ids,_=prepare_plan(plan,{'tasks':[old.model_dump()]},'取消报价',original_inputs=['取消报价'])
        self.assertEqual(tasks[0].status,'cancelled')

    def test_empty_catalog_wording_still_requires_an_empty_sql_oracle(self):
        from evals.inflight_answers import product_answer_checks,outcome
        text='当前样例目录未记录符合本次条件的有效标价商品。'
        self.assertTrue(outcome(product_answer_checks(text,{'products':[]}))['passed'])
        self.assertFalse(outcome(product_answer_checks(text,{'products':[{'product_id':1}],'quantity':1}))['passed'])

    def test_informational_variant_is_not_invented_as_in_budget(self):
        from product_variants import render_variants
        result={'variant_axis':'color','anchor':{'product_id':989,'list_price':'539.99'},'price_filters':{'max_price':550},
            'products':[{'product_id':984,'name':'Silver','color':'Silver','size':'40','list_price':'564.99','price_known':True,'within_price':False}],
            'strict':False,'returned_count':1,'matched_count':1}
        self.assertIn('不符合原价格条件',render_variants(result))

    def test_compound_bicycle_modifier_does_not_override_component_head(self):
        from product_taxonomy import mentions
        for text,expected in [('旅行车把','handlebar'),('山地车轮','wheel'),('公路车座','saddle')]:
            with self.subTest(text=text):self.assertEqual({m[2] for m in mentions(text)},{expected})
        self.assertEqual(mentions('骑行服装'),[])

    def test_scope_reset_evidence_can_be_separate_from_price_fragment_in_one_task(self):
        old=bind('头盔50以内',update={'source_index':0,'source_text':'头盔50以内','expression':expression(('price','lte',50))})
        text='改查整个目录最便宜的五条，排除0标价，不再限头盔'
        t=bind(text,previous=old,update={'source_index':0,'source_text':'排除0标价','operation':'replace','expression':expression(('price','gt',0))})
        self.assertTrue(matches({'product_subcategory_id':1,'list_price':3000},t.parameters.catalog_filter))
        self.assertEqual(t.parameter_sources['catalog_filter']['scope_source_text'],text)

    def test_unanchored_quote_cannot_use_an_invented_sku(self):
        with self.assertRaises(ConstraintConflict):
            bind('这款买两件',kind='quote',params={'product_ids':[989],'quantity':2})

    def test_successful_display_is_allowed_as_quote_reference(self):
        old=ConversationTask(kind='product_detail',status='completed',parameters=TaskParameters(product_ids=[989]),displayed_product_ids=[989])
        t=bind('这款买两件',kind='quote',previous=old,params={'product_ids':[989],'quantity':2})
        self.assertEqual(t.parameters.quote_items,[QuoteLine(product_id=989,quantity=2)])
        t=bind('这款买两件',kind='quote',previous=old,params={'quote_items':[{'product_id':989,'quantity':2}]})
        self.assertEqual(t.parameters.quote_items,[QuoteLine(product_id=989,quantity=2)])

    def test_separate_quote_tasks_do_not_borrow_each_others_lines(self):
        text='商品989两件单独报价，再给商品984一件单独报价'
        plan=TaskPlan(tasks=[PlannedTask(next='sales',task_type='quote',ref=ref,parameters=PlannerParameters(product_ids=[pid],quantity=qty))
            for ref,pid,qty in [('T1',989,2),('T2',984,1)]])
        tasks,_,_=prepare_plan(plan,{},text,original_inputs=[text])
        self.assertEqual([[x.model_dump() for x in t.parameters.quote_items] for t in tasks],
            [[{'product_id':989,'quantity':2}],[{'product_id':984,'quantity':1}]])

    def test_adjacent_chinese_and_english_type_name(self):
        from product_taxonomy import mentions
        self.assertEqual({m[2] for m in mentions('列出三款helmet')},{'helmet'})
        self.assertEqual(mentions('wheelchair'),[])

    def test_combined_quote_is_independent_of_per_item_quantity_wording(self):
        self.assertEqual(recognized('商品989和984各买两件，合并算目录小计')[0]['quote_grouping'],'combined')

    def test_new_existence_query_may_choose_valid_read_count_mode(self):
        text='我只看标价大于0且不超过1的商品，目录中有吗？'
        item=PlannedTask(next='sales',task_type='catalog_query',parameters=PlannerParameters(operation='count'),
            field_updates=[{'field':'operation','value':'count','source_index':0,'source_text':'目录中有吗'}])
        tasks,_,_=prepare_plan(TaskPlan(tasks=[item]),{},text,original_inputs=[text])
        self.assertEqual(tasks[0].parameters.operation,'count')
        self.assertEqual(tasks[0].parameters.max_price,1)

    def test_range_values_already_verified_by_normalizer_are_not_rejected_by_a_second_cue_list(self):
        old=bind('自行车预算600以内',params={'max_price':600})
        text='刚才说错了，改成500到1000之间的自行车'
        t=bind(text,previous=old,update={'source_index':0,'source_text':text,'operation':'replace',
            'expression':expression(('category','eq','bicycles'),('price','gte',500),('price','lte',1000))})
        self.assertTrue(matches({'product_subcategory_id':1,'list_price':700},t.parameters.catalog_filter))
        self.assertFalse(matches({'product_subcategory_id':1,'list_price':1100},t.parameters.catalog_filter))


if __name__=='__main__':unittest.main()
