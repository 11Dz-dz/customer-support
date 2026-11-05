"""Comparative filters use displayed catalog evidence, never guessed model prices."""
import json
import unittest
from langchain_core.messages import AIMessage,ToolMessage
from task_state import ConversationTask,TaskParameters,TaskPlan,PlannedTask,FieldUpdate
from task_scheduler import prepare_plan,PlanConflict
from constraint_updates import ConstraintConflict,fields_for_kind
from relative_price import relative_request
from retrieval_pipeline import candidate_pool


def saved(*,ids=(959,),price='742.3500',budget=None):
    task=ConversationTask(kind='product_search',agent='sales',status='completed',query='推荐一款适合周末骑行的自行车整车',
        parameters=TaskParameters(product_category='bicycles',limit=1,keyword='周末骑行',max_price=budget),
        displayed_product_ids=list(ids),evidence_call_ids=['old-evidence'])
    rows=[{'product_id':pid,'name':'Touring bike','list_price':price,'product_subcategory_id':3} for pid in ids]
    messages=[AIMessage(content='',tool_calls=[{'name':'search_products','id':'old-evidence','args':{'query':'周末骑行'}}]),
        ToolMessage(name='search_products',tool_call_id='old-evidence',content=json.dumps({'success':True,'products':rows}))]
    return task,messages


def follow(task,messages,text,*,kind='product_search',updates=(),**parameters):
    item=PlannedTask(next='sales',task_type=kind,continuation=True,target_task_id=task.task_id,
        query='推荐一款适合周末骑行且符合追加价格条件的自行车整车',parameters=TaskParameters(**parameters),field_updates=list(updates))
    tasks,_,_=prepare_plan(TaskPlan(tasks=[item]),{'tasks':[task.model_dump()],'messages':messages},text,original_inputs=[text])
    return next(t for t in tasks if t.task_id==task.task_id)


class RelativePriceTests(unittest.TestCase):
    def test_comparative_vocabulary_and_absolute_superlative(self):
        for text,direction in [('有没有比这个更便宜的','lt'),('贵一点的呢','gt'),('不比这个贵的','le'),('cheaper than this','lt')]:
            self.assertEqual(relative_request([text])[0],direction)
        self.assertIsNone(relative_request(['推荐最便宜的商品']))

    def test_model_sort_and_guessed_price_do_not_block_or_set_relative_bound(self):
        task,messages=saved();text='有没有比这个更便宜的'
        updates=[FieldUpdate(field=key,value=value,source_index=0,source_text=text) for key,value in [('sort_by','price'),('sort_order','asc'),('max_price',700)]]
        new=follow(task,messages,text,updates=updates,max_price=700,sort_by='price',sort_order='asc')
        self.assertEqual(new.parameters.max_price,742.3499)
        self.assertEqual((new.parameters.product_category,new.parameters.limit,new.parameters.keyword),('bicycles',1,'周末骑行'))
        self.assertIsNone(new.parameters.sort_by)
        self.assertEqual(new.parameter_sources['max_price']['reference_product_id'],959)
        self.assertEqual(new.parameter_sources['max_price']['reference_call_id'],'old-evidence')
        self.assertEqual(task.displayed_product_ids,[959])

    def test_wrong_catalog_route_retains_semantic_recommendation(self):
        task,messages=saved()
        new=follow(task,messages,'比这个便宜的',kind='catalog_query',sort_by='price',sort_order='asc')
        self.assertEqual(new.kind,'product_search');self.assertEqual(new.parameters.max_price,742.3499)

    def test_strict_boundary_excludes_equal_price_candidates(self):
        task,messages=saved();new=follow(task,messages,'更便宜的')
        rows=[{'product_id':1,'name':'lower','product_subcategory_id':3,'list_price':'742.3499'},
              {'product_id':2,'name':'equal','product_subcategory_id':3,'list_price':'742.3500'}]
        pool=candidate_pool([('search_products',{'success':True,'products':rows})],new)
        self.assertEqual([p['product_id'] for p in pool],[1])

    def test_higher_and_inclusive_prices(self):
        task,messages=saved()
        self.assertEqual(follow(task,messages,'更贵一点的').parameters.min_price,742.3501)
        self.assertEqual(follow(task,messages,'不比这个贵的').parameters.max_price,742.35)

    def test_explicit_budget_and_facets_still_apply(self):
        task,messages=saved(budget=1000);task.parameters.color='Blue';task.parameters.size='58'
        new=follow(task,messages,'比这个便宜，预算500以内',max_price=500)
        self.assertEqual((new.parameters.max_price,new.parameters.color,new.parameters.size),(500,'Blue','58'))

    def test_ambiguous_product_is_not_guessed_from_model_id(self):
        task,messages=saved(ids=(959,960))
        with self.assertRaises(ConstraintConflict):follow(task,messages,'有没有比这个便宜的',product_ids=[959])
        self.assertEqual(follow(task,messages,'比第一个更便宜的').parameter_sources['max_price']['reference_product_id'],959)

    def test_missing_or_failed_evidence_cannot_be_replaced_by_assistant_text(self):
        task,messages=saved()
        with self.assertRaises(ConstraintConflict):follow(task,[AIMessage(content='商品959价格742.35')],'更便宜的')
        messages[-1]=ToolMessage(name='search_products',tool_call_id='old-evidence',content=json.dumps({'success':False,'products':[{'product_id':959,'list_price':742.35}]}))
        with self.assertRaises(ConstraintConflict):follow(task,messages,'更便宜的')

    def test_unknown_reference_and_conflicting_directions_clarify(self):
        task,messages=saved()
        with self.assertRaises(ConstraintConflict):follow(task,messages,'比商品123更便宜的')
        with self.assertRaises(ConstraintConflict):relative_request(['更贵又更便宜的'])

    def test_conflicting_customer_budget_is_not_removed(self):
        task,messages=saved(budget=500)
        with self.assertRaises(ConstraintConflict):follow(task,messages,'更贵的')

    def test_previous_derived_comparison_does_not_become_permanent_budget(self):
        task,messages=saved(budget=1000)
        task.parameter_sources['max_price']={'verified_by':'business_normalizer','source_text':'预算1000以内'}
        first=follow(task,messages,'更便宜的')
        first.displayed_product_ids=[959]
        second=follow(first,messages,'更贵的')
        self.assertEqual((second.parameters.min_price,second.parameters.max_price),(742.3501,1000))
        self.assertEqual(second.parameter_sources['max_price']['source_text'],'预算1000以内')

    def test_product_search_does_not_own_catalog_sort_fields(self):
        self.assertNotIn('sort_by',fields_for_kind('product_search'))
        self.assertIn('sort_by',fields_for_kind('catalog_query'))

    def test_catalog_relative_bound_does_not_invent_a_sort_change(self):
        task,messages=saved();task.kind='catalog_query';task.parameters.sort_by='name'
        text='比这个便宜的'
        update=FieldUpdate(field='sort_by',value='price',source_index=0,source_text=text)
        new=follow(task,messages,text,kind='catalog_query',updates=[update],sort_by='price')
        self.assertEqual((new.parameters.sort_by,new.parameters.max_price),('name',742.3499))

    def test_unspecified_difference_or_percentage_is_not_invented(self):
        with self.assertRaises(ConstraintConflict):relative_request(['比这个便宜100元'])
        with self.assertRaises(ConstraintConflict):relative_request(['比这个便宜20%'])

    def test_relative_audit_respects_persisted_history_limit(self):
        from relative_price import apply_bound
        task,_=saved();task.parameter_changes=[{'field':'color','action':'test'} for _ in range(30)]
        apply_bound(task,{'field':'max_price','value':742.3499})
        self.assertEqual(len(task.parameter_changes),30)
        self.assertEqual(task.parameter_changes[-1]['action'],'relative_price')
        ConversationTask.model_validate(task.model_dump())


if __name__=='__main__':unittest.main()
