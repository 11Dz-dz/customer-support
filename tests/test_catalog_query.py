"""General Boolean filter contract, independent SQL oracle, patches and no silent widening."""
import json,random,unittest
from unittest.mock import patch
from pydantic import ValidationError
from backend.catalog.catalog_query import CatalogExpression,CatalogPredicate,amend,sql,matches,effective,render_overview
from backend.orders.adventureworks_service import AdventureWorksService
from backend.postgres_config import connect_database
from backend.tasks.task_state import PlannedTask,TaskPlan,TaskParameters,ConversationTask
from backend.tasks.task_scheduler import prepare_plan
from backend.tasks.constraint_updates import ConstraintConflict

def pred(field,operator,*values):return {'field':field,'operator':operator,'values':list(values)}
def expr(*groups):return CatalogExpression.model_validate({'groups':[{'conditions':g} for g in groups]})
def plan(text,expression,old=None,operation='amend',clear=(),kind='catalog_query',**params):
    item=PlannedTask(next='sales',task_type=kind,continuation=bool(old),target_task_id=old.task_id if old else None,
        query=text,parameters=TaskParameters(**params),query_update={'operation':operation,'expression':expression,
        'clear_fields':list(clear),'source_index':0,'source_text':text})
    tasks,ids,_=prepare_plan(TaskPlan(tasks=[item]),{'tasks':[old.model_dump()]} if old else {},text,original_inputs=[text])
    return next(t for t in tasks if t.task_id in ids)

class CatalogQueryTests(unittest.TestCase):
    def setUp(self):self.service=AdventureWorksService()

    def test_excluded_category_never_becomes_included_by_keyword_normalizer(self):
        e=expr([pred('category','not_in','bicycles')]);t=plan('除了自行车还有什么交通工具',e,operation='replace',catalog_subject='transport')
        self.assertIsNone(t.parameters.product_category);self.assertEqual(t.parameters.catalog_filter,e)
        r=self.service.query_products(operation='overview',catalog_subject='transport',catalog_filter=e)
        self.assertEqual(r['categories'],[]);self.assertGreater(r['unclassified_count'],0)
        answer=render_overview(r);self.assertIn('交通工具',answer);self.assertIn('无法仅凭',answer);self.assertNotIn('头盔',answer)

    def test_sql_and_python_agree_for_random_boolean_filters_and_nulls(self):
        random.seed(38)
        atoms=[pred('category','in','bicycles'),pred('category','not_in','bicycles'),pred('color','in','Red','Silver'),
            pred('color','not_in','Black'),pred('size','ne','40'),pred('price','lte',600),pred('price','gt',500),pred('price','eq',0)]
        with connect_database() as c:
            rows=c.execute('SELECT * FROM product_catalog').fetchall()
            for _ in range(40):
                e=expr(*[random.sample(atoms,random.randint(1,3)) for _ in range(random.randint(1,3))]);where,args=sql(e)
                actual={r['product_id'] for r in c.execute('SELECT product_id FROM product_catalog WHERE '+where,args).fetchall()}
                expected={r['product_id'] for r in rows if matches(r,e)}
                self.assertEqual(actual,expected,e.model_dump())

    def test_contradictory_conditions_require_clarification(self):
        with self.assertRaises(ConstraintConflict):plan('只看黑色但不要黑色',expr([pred('color','eq','Black'),pred('color','not_in','Black')]))
        with self.assertRaises(ConstraintConflict):plan('预算500以内但至少1000',expr([pred('price','lte',500),pred('price','gte',1000)]))

    def test_values_are_bound_parameters_not_sql(self):
        e=expr([pred('color','ne',"Black');DROP TABLE products;--")]);where,args=sql(e,alias='p.')
        self.assertNotIn('DROP',where);self.assertIn('drop',str(args).lower())
        with self.assertRaises(ValueError):sql(e,alias='unsafe;')

    def test_invalid_fields_operators_values_and_complexity_rejected(self):
        for value in [pred('order_id','eq',1),pred('category','in','cars'),pred('price','in',1),pred('color','lt','Black'),pred('price','lte',-1),pred('price','lte','nan')]:
            with self.subTest(value=value),self.assertRaises((ValueError,ValidationError)):expr([value])
        with self.assertRaises(ValueError):expr(*[[pred('color','eq','Red')]]*5)

    def test_multi_turn_amend_preserves_category_budget_and_original_task(self):
        first=plan('自行车预算600以内',expr([pred('category','eq','bicycles'),pred('price','lte',600)]),operation='replace')
        before=first.model_dump(mode='json');second=plan('不要黑色',expr([pred('color','not_in','Black')]),first)
        self.assertEqual(first.model_dump(mode='json'),before)
        self.assertTrue(matches({'product_subcategory_id':1,'list_price':550,'color':'Silver'},second.parameters.catalog_filter))
        self.assertFalse(matches({'product_subcategory_id':1,'list_price':550,'color':'Black'},second.parameters.catalog_filter))
        self.assertFalse(matches({'product_subcategory_id':1,'list_price':650,'color':'Silver'},second.parameters.catalog_filter))

    def test_new_task_does_not_inherit_other_task_budget(self):
        first=plan('自行车预算600以内',expr([pred('category','eq','bicycles'),pred('price','lte',600)]),operation='replace')
        second=plan('看看红色商品',expr([pred('color','eq','Red')]),operation='replace')
        self.assertIsNone(second.parameters.max_price);self.assertIsNone(second.parameters.product_category)

    def test_continuation_cannot_replace_whole_saved_expression(self):
        first=plan('自行车预算600以内',expr([pred('category','eq','bicycles'),pred('price','lte',600)]),operation='replace')
        patched=plan('不要黑色',expr([pred('color','not_in','Black')]),first,operation='replace')
        self.assertFalse(matches({'product_subcategory_id':1,'list_price':700,'color':'Red'},patched.parameters.catalog_filter))
        self.assertFalse(matches({'product_subcategory_id':31,'list_price':35,'color':'Red'},patched.parameters.catalog_filter))
        self.assertTrue(matches({'product_subcategory_id':1,'list_price':500,'color':'Red'},patched.parameters.catalog_filter))

    def test_repeated_saved_price_is_not_a_new_price_edit(self):
        old=plan('自行车预算600以内',expr([pred('category','eq','bicycles'),pred('price','lte',600)]),operation='replace')
        new=plan('不要黑色',expr([pred('category','in','bicycles'),pred('price','lte',600),pred('color','not_in','Black')]),old)
        self.assertEqual(new.parameters.max_price,600)
        self.assertFalse(matches({'product_subcategory_id':1,'list_price':550,'color':'Black'},new.parameters.catalog_filter))

    def test_repeated_old_price_cannot_ignore_explicit_new_budget(self):
        old=plan('预算600以内',expr([pred('price','lte',600)]),operation='replace')
        with self.assertRaises(ConstraintConflict):plan('预算提高到700',expr([pred('price','lte',600)]),old)

    def test_explicit_unlimited_clears_negative_color_even_if_model_omits_clear_list(self):
        old=plan('非黑色自行车600以内',expr([pred('category','eq','bicycles'),pred('price','lte',600),pred('color','not_in','Black')]),operation='replace')
        new=plan('颜色不限，预算不变',expr([pred('category','eq','bicycles'),pred('price','lte',600)]),old)
        self.assertTrue(matches({'product_subcategory_id':1,'color':'Black','list_price':550},new.parameters.catalog_filter))
        self.assertEqual(new.parameters.max_price,600)

    def test_new_budget_replaces_relative_price_provenance(self):
        old=plan('预算600以内',expr([pred('price','lte',600)]),operation='replace')
        old.parameter_sources['max_price']={'verified_by':'displayed_tool_price','base_bound':None}
        new=plan('预算提高到700',expr([pred('price','lte',700)]),old)
        self.assertEqual(new.parameter_sources['max_price']['verified_by'],'typed_query_contract')
        self.assertEqual(new.parameters.max_price,700)

    def test_relative_bound_intersects_every_alternative_without_widening_original_budgets(self):
        from backend.catalog.relative_price import apply_bound
        old=ConversationTask(kind='product_search',parameters=TaskParameters(catalog_filter=expr(
            [pred('category','eq','bicycles'),pred('color','eq','Red'),pred('price','lte',500)],
            [pred('category','eq','bicycles'),pred('color','eq','Silver'),pred('price','lte',800)])))
        result=apply_bound(old,{'field':'max_price','value':742})
        self.assertFalse(matches({'product_subcategory_id':1,'color':'Red','list_price':600},result.parameters.catalog_filter))
        self.assertTrue(matches({'product_subcategory_id':1,'color':'Silver','list_price':740},result.parameters.catalog_filter))
        self.assertFalse(matches({'product_subcategory_id':1,'color':'Silver','list_price':750},result.parameters.catalog_filter))
        result=apply_bound(result,{'field':'max_price','value':700})
        self.assertFalse(matches({'product_subcategory_id':1,'color':'Red','list_price':600},result.parameters.catalog_filter))
        self.assertEqual(len(result.parameters.catalog_filter.groups[0].conditions),4)

    def test_two_scopes_cannot_reuse_one_unqualified_condition_span(self):
        items=[PlannedTask(next='sales',task_type='catalog_query',ref=ref,query_update={'operation':'replace','source_index':0,'source_text':'预算500以内','expression':expr([pred('price','lte',500)])}) for ref in ['T1','T2']]
        with self.assertRaises(ConstraintConflict):prepare_plan(TaskPlan(tasks=items),{},'预算500以内',original_inputs=['预算500以内'])

    def test_explicit_positive_scope_cannot_be_replaced_by_complement(self):
        with self.assertRaises(ConstraintConflict):plan('只看自行车配件，不要自行车整车',expr([pred('category','not_in','bicycles')]),operation='replace')

    def test_positive_color_is_not_satisfied_by_merely_excluding_another_color(self):
        with self.assertRaises(ConstraintConflict):plan('只看红色商品，不要黑色',expr([pred('color','not_in','Black')]),operation='replace')

    def test_negative_scope_is_never_forced_to_positive(self):
        task=plan('只看不是黑色的商品',expr([pred('color','not_in','Black')]),operation='replace')
        self.assertIsNone(task.parameters.color)

    def test_replace_color_and_clear_budget_preserve_other_conditions(self):
        old=plan('黑色自行车600以内',expr([pred('color','eq','Black'),pred('category','eq','bicycles'),pred('price','lte',600)]),operation='replace')
        new=plan('红色或者银色',expr([pred('color','in','Red','Silver')]),old)
        cleared=plan('预算不限',expr(),new,operation='clear',clear=['price'])
        self.assertIsNone(cleared.parameters.max_price)
        self.assertTrue(matches({'color':'Silver','product_subcategory_id':1,'list_price':5000},cleared.parameters.catalog_filter))
        self.assertFalse(matches({'color':'Black','product_subcategory_id':1,'list_price':500},cleared.parameters.catalog_filter))

    def test_ungrounded_price_and_wrong_source_rejected(self):
        with self.assertRaises(ConstraintConflict):plan('预算500以内',expr([pred('price','lte',700)]))
        item=PlannedTask(next='sales',task_type='catalog_query',query_update={'source_index':0,'source_text':'模型改写','expression':expr([pred('color','eq','Red')])})
        with self.assertRaises(ConstraintConflict):prepare_plan(TaskPlan(tasks=[item]),{},'不要黑色',original_inputs=['不要黑色'])

    def test_exact_catalog_filter_and_order_come_from_database(self):
        e=expr([pred('category','eq','bicycles'),pred('color','not_in','Black'),pred('price','lte',1000)])
        result=self.service.query_products(catalog_filter=e,limit=20)
        self.assertTrue(result['products']);self.assertTrue(all(matches(p,e) for p in result['products']))
        with connect_database() as c:
            rows=c.execute("SELECT product_id FROM product_catalog WHERE product_subcategory_id=ANY(%s) AND color IS NOT NULL AND lower(color)<>'black' AND list_price>0 AND list_price<=1000 ORDER BY list_price,product_id LIMIT 20",([1,2,3],)).fetchall()
        self.assertEqual([p['product_id'] for p in result['products']],[p['product_id'] for p in rows])

    def test_keyword_and_vector_share_exclusions(self):
        e=expr([pred('category','eq','bicycles'),pred('color','not_in','Black'),pred('price','lte',1000)])
        r=self.service.search_products('bicycle',keyword_terms=['bike','bicycle'],catalog_filter=e)
        self.assertEqual(r['retrieval_mode'],'hybrid');self.assertTrue(r['products']);self.assertTrue(all(matches(p,e) for p in r['products']))

    def test_full_graph_overview_skips_recommendation_agent(self):
        import backend.agent.langgraph_supervisor_1 as project
        from evals.fixtures import CaseFixture
        from backend.conversation.chat_runtime import invoke_turn,final_reply
        with CaseFixture('owner') as f,patch.object(project,'supervisor_router') as router,patch.object(project.order_service,'search_products',side_effect=AssertionError('No semantic search')):
            router.invoke.return_value=TaskPlan(tasks=[PlannedTask(next='sales',task_type='catalog_query',parameters=TaskParameters(operation='overview',catalog_subject='transport'),query_update={'operation':'replace','source_index':0,'source_text':'除了自行车还有什么交通工具','expression':expr([pred('category','not_in','bicycles')])})])
            result=invoke_turn(project.app,'除了自行车还有什么交通工具',f.thread_id)
            answer=final_reply(result['messages']).content;self.assertIn('未分类',answer);self.assertNotIn('推荐',answer)

    def test_tool_guard_rejects_dropped_negative_filter(self):
        import backend.agent.langgraph_supervisor_1 as project
        from backend.agent.tool_execution import TurnPolicy,tool_execution_scope
        from tests.test_tool_execution import ToolExecutionTest
        task=plan('不要黑色',expr([pred('color','not_in','Black')]),operation='replace')
        with tool_execution_scope(TurnPolicy(task=task)),patch.object(project.order_service,'query_products') as service:
            messages=ToolExecutionTest.run_calls([project.query_products],[('query_products',{})])
        service.assert_not_called();self.assertEqual(json.loads(messages[0].content)['error_code'],'TASK_TOOL_NOT_ALLOWED')

if __name__=='__main__':unittest.main()
