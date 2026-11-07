"""Wrong planner outputs, grounded patches, inheritance and real SQL boundaries."""
import os
import unittest
from unittest.mock import patch
from backend.tasks.task_state import ConversationTask,PlannedTask,TaskParameters,TaskPlan,FieldUpdate,RouteDecision,prepare_task
from backend.tasks.task_scheduler import prepare_plan
from backend.tasks.constraint_updates import ConstraintConflict,recognized
from backend.agent.deepseek_model import provider_settings,provider_body


def update(field,value,text,index=0,operation='set'):
    return FieldUpdate(field=field,value=value,source_index=index,source_text=text,operation=operation)


def old_task(**kwargs):
    return ConversationTask(kind='product_search',agent='sales',parameters=TaskParameters(
        **{'product_category':'bicycles','limit':1,'max_price':1000,**kwargs}))


def apply(old,inputs,updates=(),**params):
    item=PlannedTask(next='sales',task_type=old.kind,continuation=True,target_task_id=old.task_id,
        query='model-written query',parameters=TaskParameters(**params),field_updates=list(updates))
    tasks,_,_=prepare_plan(TaskPlan(tasks=[item]),{'tasks':[old.model_dump()]},inputs[-1],original_inputs=inputs)
    return next(t for t in tasks if t.task_id==old.task_id)


class ConstraintUpdateTest(unittest.TestCase):
    def test_budget_only_preserves_all_other_constraints(self):
        old=old_task(include_variants=True,color='Black',size='42')
        new=apply(old,['预算500以内'],[update('max_price',500,'预算500以内')],max_price=500)
        self.assertEqual(new.parameters.model_dump(),old.parameters.model_copy(update={'max_price':500}).model_dump())
        self.assertEqual(new.parameter_sources['max_price']['source_text'],'预算500以内')

    def test_wrong_valid_category_does_not_replace_user_object(self):
        new=apply(old_task(),['预算500以内'],max_price=500,product_category='accessories',limit=5)
        self.assertEqual((new.parameters.product_category,new.parameters.limit,new.parameters.max_price),('bicycles',1,500))
        self.assertTrue(any(a['action']=='ignored_ungrounded_proposal' for a in new.parameter_changes))

    def test_model_cannot_broaden_category_to_all(self):
        self.assertEqual(apply(old_task(),['预算500以内'],product_category='all').parameters.product_category,'bicycles')

    def test_first_request_corrects_wrong_category_from_raw_input(self):
        item=PlannedTask(next='sales',task_type='product_search',parameters=TaskParameters(product_category='accessories',limit=5,max_price=800))
        tasks,_,_=prepare_plan(TaskPlan(tasks=[item]),{},'推荐一款自行车，预算500以内',original_inputs=['推荐一款自行车，预算500以内'])
        self.assertEqual((tasks[0].parameters.product_category,tasks[0].parameters.max_price,tasks[0].parameters.limit),('bicycles',500,1))

    def test_explicit_category_change_is_allowed(self):
        new=apply(old_task(),['不看自行车了，改成头盔'],[update('product_category','accessories','改成头盔')],product_category='accessories')
        self.assertEqual(new.parameters.product_category,'accessories')

    def test_category_quote_cannot_drop_negation(self):
        new=apply(old_task(),['不要自行车，改成头盔'],[update('product_category','bicycles','自行车')])
        self.assertEqual(new.parameters.product_category,'accessories')
        self.assertEqual(new.parameters.max_price,1000)
        self.assertEqual(new.parameter_sources['product_category']['source_text'],'不要自行车，改成头盔')

    def test_unrelated_budget_clear_does_not_block_grounded_category_change(self):
        old=old_task();text='不看自行车了，换成头盔'
        new=apply(old,[text],[update('product_category','accessories','换成头盔'),
            update('max_price',None,text,operation='clear')],product_category='accessories')
        self.assertEqual(new.parameters.product_category,'accessories')
        self.assertEqual(new.parameters.max_price,1000)
        self.assertTrue(any(c['action']=='ignored_unrelated_patch' for c in new.parameter_changes))

    def test_changed_object_does_not_keep_old_selected_product(self):
        old=old_task(product_ids=[958]);old.displayed_product_ids=[958]
        new=apply(old,['换成头盔'],[update('product_category','accessories','换成头盔')])
        self.assertEqual(new.parameters.product_ids,[]);self.assertEqual(new.displayed_product_ids,[])
        self.assertEqual(old.parameters.product_ids,[958])

    def test_new_budget_cannot_be_mislabelled_as_retry_of_empty_result(self):
        old=old_task(max_price=500);old.status='empty'
        item=PlannedTask(next='sales',task_type='product_search',retry_failed=True,
            target_task_id=old.task_id,parameters=TaskParameters(max_price=1000),
            field_updates=[update('max_price',1000,'预算提高到1000以内')])
        tasks,_,_=prepare_plan(TaskPlan(tasks=[item]),{'tasks':[old.model_dump()]},'预算提高到1000以内',original_inputs=['预算提高到1000以内'])
        new=tasks[0];self.assertFalse(new.retry_failed);self.assertEqual(new.parameters.max_price,1000)

    def test_modifier_category_uses_imported_catalog_head(self):
        self.assertEqual(recognized('公路车内胎')[0]['product_category'],'accessories')
        self.assertEqual(recognized('推荐公路竞赛车')[0]['product_category'],'bicycles')
        self.assertNotIn('product_category',recognized('自行车和头盔')[0])

    def test_catalog_price_order_and_size_units(self):
        self.assertEqual(recognized('目录标价最高')[0]['sort_order'],'desc')
        self.assertEqual(recognized('44码')[0]['size'],'44')
        self.assertEqual(recognized('有多少条')[0]['operation'],'count')

    def test_lowest_five_does_not_become_one(self):
        fields,_=recognized('列出标价最低的五个商品记录，排除0标价')
        self.assertEqual(fields['limit'],5);self.assertFalse(fields['include_zero_price'])

    def test_ambiguous_budget_cannot_use_a_guessed_explicit_task_target(self):
        old=[old_task(),old_task(product_category='accessories')]
        item=PlannedTask(next='sales',task_type='product_search',continuation=True,target_task_id=old[0].task_id,
            parameters=TaskParameters(max_price=500),field_updates=[update('max_price',500,'预算500以内')])
        with self.assertRaises(ConstraintConflict):prepare_plan(TaskPlan(tasks=[item]),{'tasks':[t.model_dump() for t in old]},
            '预算500以内',original_inputs=['预算500以内'])

    def test_size_number_is_a_catalog_string(self):
        self.assertEqual(TaskParameters(size=44).size,'44')

    def test_long_provenance_does_not_duplicate_raw_messages_into_router_context(self):
        import json
        from backend.agent.context_budget import compact_task_record
        task=old_task();text='private-source-'*1500
        task.parameter_sources={key:{'source_text':text,'source_message_text':text,'source_index':0,'operation':'set','revision':1} for key in ['color','size','max_price','product_category']}
        before=task.model_dump();view=compact_task_record(task)
        self.assertLess(len(json.dumps(view)),4000)
        self.assertNotIn('private-source-',json.dumps(view))
        self.assertEqual(task.model_dump(),before)

    def test_recommendation_count_and_quote_quantity_have_separate_scopes(self):
        old=old_task()
        search=PlannedTask(next='sales',task_type='product_search',ref='T1',continuation=True,target_task_id=old.task_id,
            parameters=TaskParameters(max_price=500,limit=5,quantity=2))
        quote=PlannedTask(next='sales',task_type='quote',ref='T2',depends_on=['T1'],dependency_mode='selected_product',
            parameters=TaskParameters(quantity=2))
        tasks,_,_=prepare_plan(TaskPlan(tasks=[search,quote]),{'tasks':[old.model_dump()]},'预算500以内，每款买两件',original_inputs=['预算500以内，每款买两件'])
        by_kind={t.kind:t for t in tasks}
        self.assertEqual(by_kind['product_search'].parameters.limit,1)
        self.assertIsNone(by_kind['product_search'].parameters.quantity)
        self.assertEqual(by_kind['quote'].parameters.quantity,2)
        self.assertIsNone(by_kind['quote'].parameters.max_price)

    def test_overlapping_vehicle_modifier_does_not_change_handlebar_class(self):
        self.assertEqual(recognized('入门旅行车把')[0]['product_category'],'components')
        self.assertEqual(recognized('山地车架')[0]['product_category'],'components')

    def test_positive_price_explicitly_excludes_zero(self):
        fields,_=recognized('标价大于0且不超过1')
        self.assertEqual(fields['min_price'],0);self.assertFalse(fields['include_zero_price'])

    def test_no_upper_limit_does_not_clear_existing_minimum(self):
        new=apply(old_task(min_price=600),['不设上限'],[update('max_price',None,'不设上限',operation='clear')])
        self.assertEqual(new.parameters.min_price,600);self.assertIsNone(new.parameters.max_price)

    def test_no_lower_limit_does_not_clear_existing_maximum(self):
        new=apply(old_task(min_price=600),['不设下限'],[update('min_price',None,'不设下限',operation='clear')])
        self.assertIsNone(new.parameters.min_price);self.assertEqual(new.parameters.max_price,1000)

    def test_known_missing_identifier_keeps_a_waiting_task_even_with_clarification(self):
        item=PlannedTask(next='billing',task_type='order_query',parameters=TaskParameters(order_scope='specific'))
        tasks,ids,_=prepare_plan(TaskPlan(tasks=[item],clarification='请提供有效订单号'),{},'查订单',original_inputs=['查订单'])
        self.assertEqual(tasks[0].kind,'order_query');self.assertEqual(tasks[0].status,'awaiting_input')
        self.assertEqual(tasks[0].awaiting_fields,['order_id']);self.assertEqual(ids,[tasks[0].task_id])

    def test_clarification_cannot_silently_drop_all_task_candidates(self):
        with self.assertRaises(ValueError):TaskPlan(tasks=[],clarification='请提供订单号')

    def test_unchanged_old_value_does_not_require_a_fabricated_fresh_source(self):
        old=old_task();old.parameter_sources={'max_price':{'source_text':'预算1000以内','source_index':0,'revision':1}}
        item=PlannedTask(next='sales',task_type='product_search',continuation=True,target_task_id=old.task_id,
            parameters=TaskParameters(product_category='accessories',max_price=1000),
            field_updates=[update('product_category','accessories','换成头盔'),update('max_price',1000,'预算1000以内')])
        tasks,_,_=prepare_plan(TaskPlan(tasks=[item]),{'tasks':[old.model_dump()]},'换成头盔',original_inputs=['换成头盔'])
        self.assertEqual(tasks[0].parameters.product_category,'accessories')
        self.assertEqual(tasks[0].parameters.max_price,1000)
        self.assertEqual(tasks[0].parameter_sources['max_price'],old.parameter_sources['max_price'])

    def test_each_item_purchase_has_a_per_item_quote_scope(self):
        values,_=recognized('每款买两件')
        self.assertEqual(values['quantity'],2);self.assertEqual(values['quote_grouping'],'separate')

    def test_explicit_combined_quote_overrides_per_item_presentation_default(self):
        values,_=recognized('每款买两件，合计多少钱')
        self.assertEqual(values['quantity'],2);self.assertEqual(values['quote_grouping'],'combined')

    def test_valid_quote_with_wrong_value_is_rejected(self):
        with self.assertRaises(ConstraintConflict):apply(old_task(),['预算500以内'],[update('max_price',800,'预算500以内')])

    def test_fabricated_quote_rejected(self):
        with self.assertRaises(ConstraintConflict):apply(old_task(),['预算500以内'],[update('product_category','accessories','改成头盔')])

    def test_out_of_range_message_index_rejected(self):
        with self.assertRaises(ConstraintConflict):apply(old_task(),['预算500以内'],[update('max_price',500,'预算500以内',9)])

    def test_explicit_clear_does_not_clear_other_fields(self):
        new=apply(old_task(color='Black'),['取消预算限制'],[update('max_price',None,'取消预算限制',operation='clear')])
        self.assertIsNone(new.parameters.max_price);self.assertEqual(new.parameters.color,'Black')

    def test_unrequested_clear_is_not_allowed(self):
        with self.assertRaises(ConstraintConflict):apply(old_task(),['预算500以内'],[update('max_price',None,'预算500以内',operation='clear')])

    def test_defaults_do_not_reset_include_variants_directly(self):
        old=old_task(include_variants=True)
        new=prepare_task(RouteDecision(next='sales',task_type='product_search',continuation=True,
            parameters=TaskParameters(max_price=500)),old.model_dump())
        self.assertTrue(new.parameters.include_variants)

    def test_explicit_false_is_preserved_in_model_conversion(self):
        p=PlannedTask(next='sales',task_type='product_search',parameters=TaskParameters(include_variants=False))
        self.assertEqual(p.parameters.model_fields_set,{'include_variants'})

    def test_explicit_variant_change(self):
        new=apply(old_task(include_variants=True),['不同型号，每个型号一款'],[update('include_variants',False,'不同型号')])
        self.assertFalse(new.parameters.include_variants)

    def test_quantity_not_recommendation_count(self):
        new=apply(old_task(),['预算500以内，每款买两件'])
        self.assertEqual(new.parameters.limit,1);self.assertIsNone(new.parameters.quantity)

    def test_later_raw_count_replaces_earlier(self):
        self.assertEqual(apply(old_task(),['推荐一款自行车','改成两款，预算500以内']).parameters.limit,2)

    def test_later_raw_budget_wins(self):
        self.assertEqual(apply(old_task(),['预算1000以内','预算500以内']).parameters.max_price,500)

    def test_patch_citing_old_budget_cannot_erase_new_budget(self):
        new=apply(old_task(),['预算1000以内','预算500以内'],[update('max_price',1000,'预算1000以内')])
        self.assertEqual(new.parameters.max_price,500)
        self.assertEqual(new.parameter_sources['max_price']['source_index'],1)

    def test_conflict_does_not_mutate_saved_task(self):
        old=old_task();before=old.model_dump()
        with self.assertRaises(ConstraintConflict):apply(old,['预算500以内'],[update('max_price',700,'预算500以内')])
        self.assertEqual(old.model_dump(),before)

    def test_crossed_price_range_requires_clarification(self):
        with self.assertRaises(ConstraintConflict):apply(old_task(min_price=600),['预算500以内'])

    def test_missing_color_is_inherited(self):
        self.assertEqual(apply(old_task(color='Black'),['预算500以内']).parameters.color,'Black')

    def test_color_change(self):
        self.assertEqual(apply(old_task(color='Black'),['改成红色'],[update('color','Red','改成红色')]).parameters.color,'Red')

    def test_size_change(self):
        self.assertEqual(apply(old_task(size='42'),['尺寸改成44'],[update('size','44','尺寸改成44')]).parameters.size,'44')

    def test_chinese_budget_number(self):
        self.assertEqual(apply(old_task(),['预算五百以内']).parameters.max_price,500)

    def test_composite_category_uses_longest_business_term(self):
        self.assertEqual(recognized('推荐自行车头盔')[0]['product_category'],'accessories')

    def test_unverified_new_filter_requires_clarification(self):
        item=PlannedTask(next='sales',task_type='product_search',parameters=TaskParameters(product_category='bicycles',color='Red',limit=1))
        with self.assertRaises(ConstraintConflict):prepare_plan(TaskPlan(tasks=[item]),{},'推荐一款自行车',original_inputs=['推荐一款自行车'])

    def test_shared_bare_budget_cannot_update_two_products(self):
        items=[PlannedTask(ref='T'+str(i),next='sales',task_type='product_search',field_updates=[update('max_price',500,'预算500以内')]) for i in (1,2)]
        with self.assertRaises(ConstraintConflict):prepare_plan(TaskPlan(tasks=items),{},'预算500以内',original_inputs=['预算500以内'])

    def test_old_checkpoint_without_source_fields_still_loads(self):
        data=old_task().model_dump();data.pop('parameter_sources');data.pop('parameter_changes')
        self.assertEqual(ConversationTask.model_validate(data).parameter_sources,{})


    def test_latest_price_does_not_become_a_presentation_count(self):
        new=apply(old_task(),['预算调整为1500元'])
        self.assertEqual(new.parameters.max_price,1500);self.assertEqual(new.parameters.limit,1)

    def test_at_least_two_products_is_not_minimum_price(self):
        fields,_=recognized('至少两款自行车')
        self.assertNotIn('min_price',fields)

    def test_explicit_default_false_survives_scheduler_when_supported(self):
        new=apply(old_task(include_variants=True),['只看不同型号'],
                  [update('include_variants',False,'不同型号')],include_variants=False)
        self.assertFalse(new.parameters.include_variants)

    def test_unmentioned_boolean_survives_scheduler_even_if_model_default_is_present(self):
        new=apply(old_task(include_variants=True),['预算500以内'],include_variants=False,max_price=500)
        self.assertTrue(new.parameters.include_variants)

    def test_task_source_metadata_round_trip(self):
        new=apply(old_task(),['预算500以内'],[update('max_price',500,'预算500以内')])
        loaded=ConversationTask.model_validate_json(new.model_dump_json())
        self.assertEqual(loaded.parameter_sources,new.parameter_sources)
        self.assertEqual(loaded.parameter_changes,new.parameter_changes)

    def test_atomic_top_level_clarification_preserves_previous_state(self):
        old=old_task()
        from backend.tasks.task_scheduler import PlanConflict
        with self.assertRaises(PlanConflict):prepare_plan(TaskPlan(tasks=[PlannedTask(next='sales',task_type='general')],clarification='要换哪种商品？'),
            {'tasks':[old.model_dump()]},'换一种',original_inputs=['换一种'])


class ProviderSettingsTest(unittest.TestCase):
    def test_bailian_defaults_and_body(self):
        with patch.dict(os.environ,{'DEEPSEEK_PROVIDER':'bailian'},clear=True):
            self.assertEqual(provider_settings()['model'],'deepseek-v4.1-flash-1017')
            self.assertEqual(provider_body('router'),{'enable_thinking':False})

    def test_official_defaults(self):
        with patch.dict(os.environ,{},clear=True):self.assertEqual(provider_settings()['base_url'],'https://api.deepseek.com')

    def test_provider_mismatch_is_rejected(self):
        with patch.dict(os.environ,{'DEEPSEEK_PROVIDER':'bailian','DEEPSEEK_BASE_URL':'https://api.deepseek.com'},clear=True):
            with self.assertRaises(ValueError):provider_settings()

    def test_url_with_credentials_or_redirect_host_is_rejected(self):
        for url in ['https://user:password@dashscope.aliyuncs.com/compatible-mode/v1','https://dashscope.aliyuncs.com.evil.test/v1','http://dashscope.aliyuncs.com/v1']:
            with patch.dict(os.environ,{'DEEPSEEK_PROVIDER':'bailian','DEEPSEEK_BASE_URL':url},clear=True):
                with self.assertRaises(ValueError):provider_settings()


if __name__=='__main__':unittest.main()


class FacetDatabaseTest(unittest.TestCase):
    def test_both_retrieval_paths_keep_exact_color_and_size(self):
        from backend.orders.adventureworks_service import AdventureWorksService
        from backend.postgres_config import connect_database
        result=AdventureWorksService().search_products('mountain bike',product_category='bicycles',max_price=3000,color='Black',size='42')
        self.assertTrue(result.get('success'))
        with connect_database() as c:
            allowed={r['product_id'] for r in c.execute("SELECT product_id FROM product_catalog WHERE product_subcategory_id IN (1,2,3) AND list_price>0 AND list_price<=3000 AND color='Black' AND size='42'").fetchall()}
        self.assertTrue(allowed)
        self.assertTrue(result['products'])
        self.assertTrue(all(p['product_id'] in allowed for p in result['products']))

    def test_empty_scope_uses_size_not_just_category_price(self):
        from backend.orders.adventureworks_service import AdventureWorksService
        result=AdventureWorksService().catalog_scope_summary(max_price=3000,product_category='bicycles',size='99')
        self.assertEqual(result['matched_count'],0);self.assertEqual(result['size'],'99')

    def test_exact_catalog_query_has_the_same_category_constraint(self):
        from backend.orders.adventureworks_service import AdventureWorksService
        result=AdventureWorksService().query_products(product_category='accessories',max_price=100,limit=5)
        self.assertTrue(result['products'])
        self.assertTrue(all(p['product_subcategory_id'] in range(26,38) for p in result['products']))

    def test_reused_candidates_cannot_ignore_new_facets(self):
        from backend.catalog.retrieval_pipeline import candidate_pool
        task=old_task(color='Black',size='42')
        data={'success':True,'products':[{'product_id':1,'list_price':700,'color':'Black','size':'42','product_subcategory_id':1},
            {'product_id':2,'list_price':600,'color':'Red','size':'42','product_subcategory_id':1},
            {'product_id':3,'list_price':600,'color':'Black','size':'44','product_subcategory_id':1}]}
        result=candidate_pool([('search_products',data)],task)
        self.assertEqual([p['product_id'] for p in result],[1])


class TaskToolFilterTest(unittest.TestCase):
    def test_specialist_cannot_resurrect_cleared_filters(self):
        from types import SimpleNamespace
        from langchain_core.tools import tool
        from langchain_core.messages import ToolMessage
        from backend.agent.tool_execution import TurnPolicy,guarded_tool_call,tool_execution_scope
        @tool
        def search_products(query:str,max_price:float|None=None,product_category:str|None=None,color:str|None=None,size:str|None=None)->dict:
            """Fake search, no network or DB."""
            return {'success':True}
        task=old_task(max_price=None,color=None,size=None)
        request=SimpleNamespace(tool_call={'name':'search_products','id':'test','args':{'query':'bike','max_price':500,'color':'Black','size':'42'}},tool=search_products,runtime=SimpleNamespace(config={}))
        seen=[]
        def execute(req):
            seen.append(dict(req.tool_call['args']))
            return ToolMessage(content='{"success":true}',name='search_products',tool_call_id='test')
        with tool_execution_scope(TurnPolicy(task=task)):guarded_tool_call(request,execute)
        self.assertEqual(seen,[])
        self.assertEqual(request.tool_call['args'],{'query':'bike','max_price':500,'color':'Black','size':'42'})

    def test_specialist_cannot_expand_budget_or_change_facets(self):
        from types import SimpleNamespace
        from langchain_core.tools import tool
        from langchain_core.messages import ToolMessage
        from backend.agent.tool_execution import TurnPolicy,guarded_tool_call,tool_execution_scope
        import json
        @tool
        def search_products(query:str,max_price:float|None=None,product_category:str|None=None,color:str|None=None,size:str|None=None)->dict:
            """Fake search with no network or database."""
            return {'success':True}
        task=old_task(max_price=500,color='Black',size='42')
        calls=[]
        for args in [{'query':'bike'}, {'query':'bike','max_price':5000,'product_category':'accessories','color':'Red','size':'44'}]:
            request=SimpleNamespace(tool_call={'name':'search_products','id':'test','args':args},tool=search_products,runtime=SimpleNamespace(config={}))
            def execute(req):
                calls.append(dict(req.tool_call['args']))
                return ToolMessage(content=json.dumps({'success':True}),name='search_products',tool_call_id='test')
            with tool_execution_scope(TurnPolicy(task=task)):guarded_tool_call(request,execute)
        self.assertEqual(calls,[])
        self.assertEqual(request.tool_call['args']['max_price'],5000)
        self.assertEqual(request.tool_call['args']['product_category'],'accessories')


class OriginalCallAuditTest(unittest.TestCase):
    def test_valid_authoritative_filters_execute_without_mutating_original_call(self):
        from types import SimpleNamespace
        from copy import deepcopy
        from langchain_core.tools import tool
        from langchain_core.messages import ToolMessage
        from backend.agent.tool_execution import TurnPolicy,guarded_tool_call,tool_execution_scope
        @tool
        def search_products(query:str,max_price:float|None=None,product_category:str|None=None)->dict:
            """Fake search."""
            return {'success':True}
        for task,args in [(old_task(),{'query':'bike','max_price':1000,'product_category':'bicycles'}),
                          (old_task(max_price=None),{'query':'bike','product_category':'bicycles'})]:
            call={'name':'search_products','id':'audit','args':args};before=deepcopy(call);seen=[]
            request=SimpleNamespace(tool_call=call,tool=search_products,runtime=SimpleNamespace(config={}))
            def execute(req):
                seen.append(deepcopy(req.tool_call))
                return ToolMessage(content='{"success":true}',name='search_products',tool_call_id='audit')
            with tool_execution_scope(TurnPolicy(task=task)):guarded_tool_call(request,execute)
            self.assertEqual(seen,[before]);self.assertEqual(call,before)

    def test_rejected_filter_change_retains_original_proposal_and_error_code(self):
        from types import SimpleNamespace
        from copy import deepcopy
        from langchain_core.tools import tool
        from backend.agent.tool_execution import TurnPolicy,guarded_tool_call,tool_execution_scope
        import json
        @tool
        def search_products(query:str,max_price:float|None=None,product_category:str|None=None)->dict:
            """Fake search."""
            return {'success':True}
        call={'name':'search_products','id':'audit','args':{'query':'bike','max_price':5000,'product_category':'accessories'}}
        before=deepcopy(call);request=SimpleNamespace(tool_call=call,tool=search_products,runtime=SimpleNamespace(config={}))
        with tool_execution_scope(TurnPolicy(task=old_task())):
            result=guarded_tool_call(request,lambda req:self.fail('Inconsistent filters must not execute'))
        self.assertEqual(call,before);self.assertEqual(result.status,'error')
        self.assertEqual(json.loads(result.content)['error_code'],'TASK_TOOL_NOT_ALLOWED')


class BoundedPlanRepairTest(unittest.TestCase):
    def run_plan(self,responses):
        import backend.agent.langgraph_supervisor_1 as project
        from langchain_core.messages import HumanMessage
        from backend.conversation.chat_runtime import turn_config
        from uuid import uuid4
        config=turn_config('constraint-repair-test-'+uuid4().hex)
        with patch.object(project,'invoke_model',side_effect=responses) as invoke:
            result=project.supervisor_node({'messages':[HumanMessage(content='推荐一款自行车')], 'tasks':[]},config)
        return result,invoke

    def proposal(self,**extra):
        return TaskPlan(tasks=[PlannedTask(next='sales',task_type='product_search',query='推荐一款自行车',
            parameters=TaskParameters(product_category='bicycles',limit=1,**extra))])

    def test_invalid_candidate_can_be_repaired_before_any_tools(self):
        invalid=self.proposal(color='Red');valid=self.proposal()
        result,invoke=self.run_plan([invalid,valid])
        self.assertEqual(invoke.call_count,2)
        self.assertIsNone(result['plan_message'])
        self.assertEqual(len(result['tasks']),1)
        self.assertIsNone(result['tasks'][0]['parameters']['color'])
        self.assertEqual(result['tasks'][0]['parameters']['limit'],1)
        self.assertIn('错误字段：color',str(invoke.call_args_list[1].args[1]))
        self.assertEqual(result['tasks'][0]['operations'],[])

    def test_second_invalid_candidate_is_not_retried_or_committed(self):
        result,invoke=self.run_plan([self.proposal(color='Red'),self.proposal(size='44')])
        self.assertEqual(invoke.call_count,2)
        self.assertEqual(result['tasks'],[]);self.assertEqual(result['turn_task_ids'],[])
        self.assertIn('尺寸',result['plan_message'])

    def test_schema_and_constraint_failure_share_one_repair_allowance(self):
        from langchain_core.exceptions import OutputParserException
        result,invoke=self.run_plan([OutputParserException('invalid JSON'),self.proposal(color='Red')])
        self.assertEqual(invoke.call_count,2)
        self.assertEqual(result['tasks'],[]);self.assertIn('颜色',result['plan_message'])

    def test_real_price_conflict_does_not_trigger_model_guessing(self):
        import backend.agent.langgraph_supervisor_1 as project
        from langchain_core.messages import HumanMessage
        from backend.conversation.chat_runtime import turn_config
        from uuid import uuid4
        old=old_task(min_price=600)
        candidate=PlannedTask(next='sales',task_type='product_search',continuation=True,target_task_id=old.task_id,
            parameters=TaskParameters(product_category='bicycles',limit=1,max_price=500))
        with patch.object(project,'invoke_model',return_value=TaskPlan(tasks=[candidate])) as invoke:
            out=project.supervisor_node({'messages':[HumanMessage(content='预算500以内')],'tasks':[old.model_dump()]},turn_config('constraint-conflict-'+uuid4().hex))
        self.assertEqual(invoke.call_count,1);self.assertEqual(out['tasks'],[old.model_dump()])
        self.assertIn('最低价格高于最高价格',out['plan_message'])


    def test_unsupported_explicit_condition_is_not_dropped_by_repair(self):
        import backend.agent.langgraph_supervisor_1 as project
        from langchain_core.messages import HumanMessage
        from backend.conversation.chat_runtime import turn_config
        from uuid import uuid4
        invalid=self.proposal(color='Purple')
        with patch.object(project,'invoke_model',return_value=invalid) as invoke:
            out=project.supervisor_node({'messages':[HumanMessage(content='推荐一款紫色自行车')],'tasks':[]},turn_config('constraint-unknown-'+uuid4().hex))
        self.assertEqual(invoke.call_count,1);self.assertEqual(out['tasks'],[])
        self.assertIn('颜色',out['plan_message'])


class UnpublishedReplacementTest(unittest.TestCase):
    def split(self):
        return TaskPlan(tasks=[
            PlannedTask(next='sales',task_type='product_search',ref='T1',parameters=TaskParameters(product_category='bicycles',limit=1,max_price=1000)),
            PlannedTask(next='sales',task_type='product_search',ref='T2',parameters=TaskParameters(product_category='accessories',limit=5,max_price=100))])

    def test_unpublished_replace_merges_count_and_latest_budget(self):
        plan=self.split();inputs=['推荐一款自行车，预算1000以内','换成头盔，预算100以内']
        before=plan.model_dump()
        tasks,ids,_=prepare_plan(plan,{},inputs[-1],original_inputs=inputs)
        self.assertEqual(len(tasks),1);self.assertEqual(len(ids),1)
        p=tasks[0].parameters
        self.assertEqual((p.product_category,p.max_price,p.limit),('accessories',100,1))
        self.assertEqual(plan.model_dump(),before)
        self.assertTrue(any(x['action']=='collapsed_unpublished_replacement' for x in tasks[0].parameter_changes))

    def test_additional_request_keeps_both_goals(self):
        from backend.tasks.constraint_updates import collapse_unpublished_replacement
        plan=self.split();out,changed=collapse_unpublished_replacement(plan,[],['推荐自行车','另外再推荐头盔'])
        self.assertFalse(changed);self.assertEqual(out.model_dump(),plan.model_dump())

    def test_stored_task_is_never_rebound_by_new_segment_rule(self):
        from backend.tasks.constraint_updates import collapse_unpublished_replacement
        plan=self.split();out,changed=collapse_unpublished_replacement(plan,[old_task()],['推荐一款自行车','换成头盔'])
        self.assertFalse(changed);self.assertEqual(out.model_dump(),plan.model_dump())

    def test_multiple_initial_objects_require_normal_scoped_planning(self):
        from backend.tasks.constraint_updates import collapse_unpublished_replacement
        out,changed=collapse_unpublished_replacement(self.split(),[],['推荐自行车和头盔','换成水壶'])
        self.assertFalse(changed);self.assertEqual(len(out.tasks),2)

    def test_dependency_is_not_silently_removed(self):
        from backend.tasks.constraint_updates import collapse_unpublished_replacement
        plan=self.split();plan.tasks[1].depends_on=['T1']
        out,changed=collapse_unpublished_replacement(plan,[],['推荐一款自行车','换成头盔'])
        self.assertFalse(changed);self.assertEqual(out.tasks[1].depends_on,['T1'])

    def test_replacement_can_explicitly_change_display_count(self):
        inputs=['推荐一款自行车，预算1000以内','换成两款头盔，预算100以内']
        tasks,_,_=prepare_plan(self.split(),{},inputs[-1],original_inputs=inputs)
        self.assertEqual(len(tasks),1);self.assertEqual(tasks[0].parameters.limit,2)
