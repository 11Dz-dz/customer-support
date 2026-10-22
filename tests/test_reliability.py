"""Partial completion, explicit safe retries, bounded model inputs and unmodified persistence."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from answer_contract import render_business_result, parse_answer_plan
from context_budget import (ContextBudgetError, compact_task_record, compact_tool_message,
                            expert_context, message_chars, router_context)
from task_execution import execute_read_task, seed_product_search
from task_operations import retry_operations
from task_result import collect_task_result, TaskResult, ToolEvidence
from task_scheduler import PlanConflict, prepare_plan, resolve_dependencies
from task_state import (ConversationTask, OperationRecord, PlannedTask, TaskParameters, TaskPlan, complete_task)
from tool_execution import TurnPolicy, guarded_prompt, guarded_tool_call, tool_execution_scope


def batch(rows):
    calls=[{'name':name,'args':args,'id':f'c{i}'} for i,(name,args,data) in enumerate(rows)]
    return [AIMessage(content='',tool_calls=calls),*[ToolMessage(name=name,tool_call_id=calls[i]['id'],
        content=json.dumps(data)) for i,(name,args,data) in enumerate(rows)]]


def partial_order():
    task=ConversationTask(kind='order_query',agent='billing',parameters=TaskParameters(order_id=43659),focus=['order','payment','invoice'])
    messages=[HumanMessage(content='查订单、支付、发票')]+batch([
        ('query_order',{'order_id':'43659'},{'success':True,'order_id':43659,'status_code':5,'status':'已发货'}),
        ('check_payment_status',{'order_id':'43659'},{'success':False,'error_code':'TEMPORARY_ERROR'}),
        ('query_invoice',{'order_id':'43659'},{'success':True,'order_id':43659,'invoice_status':'not_available'})])
    return complete_task(task,collect_task_result(messages,task)),messages


class PresentationConsistencyTest(unittest.TestCase):
    def test_presentation_units_normalize_separately_from_budget_and_purchase_amount(self):
        from task_state import explicit_product_count
        for query,count in [('推荐一款自行车',1),('预算100以内，挑选两款',2),('展示12个候选',12),
                            ('推荐二十款，随后按每款3件计算',20),('找一款2件装商品',1),
                            ('价格6以内，有两个轮子的车',None),('每款买3件，预算2000',None),
                            ('一款或两款均可',None),('适合两种道路条件的自行车',None)]:
            with self.subTest(query=query):self.assertEqual(explicit_product_count(query),count)
        from langgraph_supervisor_1 import validate_planner_counts
        plan=TaskPlan(tasks=[PlannedTask(next='sales',task_type='product_search',query='推荐一款车',
            parameters=TaskParameters(limit=5,product_category='all'))])
        self.assertEqual(validate_planner_counts(plan).tasks[0].parameters.limit,1)

    def test_fenced_selection_survives_limit_and_renderer(self):
        from langgraph_supervisor_1 import limit_product_plan
        result=TaskResult(current=[ToolEvidence(tool='search_products',data={'success':True,'products':[
            {'product_id':1,'name':'Accessory','list_price':10},
            {'product_id':2,'name':'Bicycle','list_price':100}]})])
        task=ConversationTask(kind='product_search',agent='sales',parameters=TaskParameters(limit=1))
        draft='```json\n{"kind":"data","focus":["products"],"product_ids":[2]}\n```'
        content,selected=limit_product_plan(task,result,draft)
        self.assertEqual(selected,[2])
        self.assertEqual(parse_answer_plan(content).product_ids,[2])
        answer,_=render_business_result(result,content,'sales')
        self.assertIn('商品2',answer)
        self.assertNotIn('商品1',answer)

    def test_candidate_pool_is_independent_of_display_limit(self):
        task=ConversationTask(kind='product_search',agent='sales',parameters=TaskParameters(limit=1))
        with patch('task_execution.execute_operations') as execute:
            seed_product_search(task,'recommend',{}, {})
        operations=execute.call_args.args[1]
        self.assertEqual(operations[0][1]['limit'],5)
        self.assertEqual(task.parameters.limit,1)

    def test_historical_application_requires_a_fresh_authorized_same_order(self):
        history=[ToolEvidence(tool='request_refund',data={'success':True,'order_id':42,
            'refund_id':'TEST','status':'pending','funds_action':'not_executed_by_this_system'})]
        fresh=ToolEvidence(tool='query_order',data={'success':True,'order_id':42,'status_code':5,'status':'已发货'})
        result=TaskResult(history=history,current=[fresh],requested_order_id=42)
        answer,_=render_business_result(result,'{"focus":["refund"]}','billing')
        self.assertIn('上次记录',answer)
        self.assertIn('未执行资金退款',answer)
        self.assertIn('本轮未重复提交',answer)
        for replacement in [ToolEvidence(tool='query_order',data={'success':True,'order_id':43}),
                            ToolEvidence(tool='query_order',data={'success':False,'error_code':'ORDER_NOT_ACCESSIBLE'})]:
            result.current=[replacement]
            answer,_=render_business_result(result,'{"focus":["refund"]}','billing')
            self.assertNotIn('TEST',answer)
            self.assertNotIn('上次记录',answer)


class PartialResultTest(unittest.TestCase):
    def test_retry_can_select_recorded_failed_objects_without_rewriting_task_scope(self):
        task=ConversationTask(kind='product_detail',agent='sales',parameters=TaskParameters(product_ids=[1,2,3]))
        messages=[HumanMessage(content='details')]+batch([
            ('get_product_info',{'product_name':'1'},{'success':True,'product':{'product_id':1}}),
            ('get_product_info',{'product_name':'2'},{'success':False,'error_code':'TEMPORARY_ERROR'}),
            ('get_product_info',{'product_name':'3'},{'success':False,'error_code':'TEMPORARY_ERROR'})])
        old=complete_task(task,collect_task_result(messages,task))
        for ids in [[2],[2,3]]:
            plan=TaskPlan(tasks=[PlannedTask(next='sales',task_type='product_detail',retry_failed=True,
                target_task_id=old.task_id,parameters=TaskParameters(product_ids=ids))])
            tasks,_,_=prepare_plan(plan,{'tasks':[old.model_dump()]},'retry')
            self.assertEqual(tasks[0].parameters.product_ids,[1,2,3])
            self.assertEqual(retry_operations(tasks[0]),[('get_product_info',{'product_name':str(i)}) for i in ids])
            if ids == [2]:
                retry_messages=messages+[HumanMessage(content='retry two'),
                    AIMessage(content='',tool_calls=[{'name':'get_product_info','args':{'product_name':'2'},'id':'retry-two'}]),
                    ToolMessage(name='get_product_info',tool_call_id='retry-two',content='{"success":true,"product":{"product_id":2}}')]
                finished=complete_task(tasks[0],collect_task_result(retry_messages,tasks[0]))
                self.assertEqual(finished.status,'partial')
                self.assertEqual(retry_operations(finished),[('get_product_info',{'product_name':'3'})])
        for ids in [[1],[4],[1,2]]:
            plan=TaskPlan(tasks=[PlannedTask(next='sales',task_type='product_detail',retry_failed=True,
                target_task_id=old.task_id,parameters=TaskParameters(product_ids=ids))])
            with self.assertRaises(PlanConflict):prepare_plan(plan,{'tasks':[old.model_dump()]},'retry')

    def test_mixed_success_is_partial_and_retains_each_operation(self):
        task,messages=partial_order()
        self.assertEqual(task.status,'partial')
        self.assertEqual([o.status for o in task.operations],['succeeded','failed','succeeded'])
        self.assertEqual(retry_operations(task),[('check_payment_status',{'order_id':'43659'})])
        result=collect_task_result(messages,ConversationTask(kind='order_query',agent='billing'))
        answer,_=render_business_result(result,'{"kind":"data","focus":["order","payment","invoice"]}','billing')
        self.assertIn('订单状态：已发货',answer)
        self.assertIn('仅部分完成',answer)
        self.assertNotIn('支付状态未知：源数据未提供',answer)

    def test_retry_merges_old_successes_and_replaces_failed_operation(self):
        old,messages=partial_order()
        plan=TaskPlan(tasks=[PlannedTask(next='billing',task_type='order_query',target_task_id=old.task_id,retry_failed=True)])
        tasks,ids,_=prepare_plan(plan,{'tasks':[old.model_dump()]},'重试失败部分')
        retry=tasks[0]
        self.assertTrue(retry.retry_failed)
        self.assertEqual(retry.focus,old.focus)
        messages+=[HumanMessage(content='重试失败部分'),AIMessage(content='',tool_calls=[{'name':'check_payment_status','args':{'order_id':'43659'},'id':'retry'}]),
                   ToolMessage(name='check_payment_status',tool_call_id='retry',content=json.dumps({'success':True,'order_id':43659,'payment_status':'unknown'}))]
        result=collect_task_result(messages,retry)
        self.assertEqual(result.status,'completed')
        self.assertEqual(len(result.retained),2)
        self.assertEqual([o.attempts for o in result.operations],[1,2,1])
        answer,_=render_business_result(result,'{"kind":"data","focus":["order","payment","invoice"]}','billing')
        self.assertIn('订单状态：已发货',answer)
        self.assertIn('支付状态未知',answer)
        self.assertIn('无法确认发票',answer)
        self.assertIn('此前已核实',answer)

    def test_tool_error_status_overrides_a_success_flag(self):
        messages=batch([('query_order',{'order_id':'1'},{'success':True})])
        messages[-1].status='error'
        result=collect_task_result([HumanMessage(content='查询')]+messages)
        self.assertEqual(result.status,'failed')

    def test_successful_unknown_payment_is_not_a_retryable_failure(self):
        result=collect_task_result([HumanMessage(content='支付')]+batch([
            ('check_payment_status',{'order_id':'1'},{'success':True,'payment_status':'unknown'})]))
        self.assertEqual(result.status,'completed')
        self.assertFalse(result.operations[0].retryable)

    def test_denial_on_retry_cannot_render_retained_order_information(self):
        old,messages=partial_order()
        old.retry_failed=True
        messages+=[HumanMessage(content='再试')]+batch([('check_payment_status',{'order_id':'43659'},
            {'success':False,'error_code':'ORDER_NOT_ACCESSIBLE'})])
        # New calls need distinct IDs from the historical batch.
        messages[-2].tool_calls[0]['id']='denial'
        messages[-1].tool_call_id='denial'
        result=collect_task_result(messages,old)
        answer,_=render_business_result(result,'{}','billing')
        self.assertEqual(result.status,'denied')
        self.assertEqual(result.retained,[])
        self.assertNotIn('已发货',answer)
        self.assertNotIn('43659',answer)

    def test_retry_attempts_are_bounded_across_turns(self):
        old,messages=partial_order()
        for i in range(2):
            old.retry_failed=True
            calls=batch([('check_payment_status',{'order_id':'43659'},{'success':False})])
            calls[0].tool_calls[0]['id']=f'retry{i}'
            calls[1].tool_call_id=f'retry{i}'
            messages += [HumanMessage(content='再试')]+calls
            old=complete_task(old,collect_task_result(messages,old))
        self.assertEqual(next(o.attempts for o in old.operations if o.tool=='check_payment_status'),3)
        self.assertEqual(retry_operations(old),[])

    def test_changed_order_is_not_treated_as_retry(self):
        old,_=partial_order()
        with self.assertRaises(PlanConflict):
            prepare_plan(TaskPlan(tasks=[PlannedTask(next='billing',task_type='order_query',target_task_id=old.task_id,
                retry_failed=True,parameters=TaskParameters(order_id=43660))]),{'tasks':[old.model_dump()]},'换订单重试')

    def test_retry_uses_ledger_owner_when_model_labels_the_failed_suboperation(self):
        old,_=partial_order()
        tasks,ids,_=prepare_plan(TaskPlan(tasks=[PlannedTask(next='billing',task_type='payment_query',retry_failed=True)]),
                                {'tasks':[old.model_dump()]},'只重试支付部分')
        self.assertEqual(ids,[old.task_id])
        self.assertEqual(tasks[0].kind,'order_query')
        self.assertEqual(retry_operations(tasks[0]),[('check_payment_status',{'order_id':'43659'})])

    def test_write_denied_completed_and_legacy_unknown_operations_are_not_replayed(self):
        for status,name,code in [('partial','request_refund',None),('denied','query_order','ORDER_NOT_ACCESSIBLE'),('completed','query_order',None)]:
            task=ConversationTask(kind='refund_request' if name=='request_refund' else 'order_query',agent='billing',status=status,
                operations=[OperationRecord(operation_id='x',tool=name,arguments={'order_id':'43659'},status='failed',
                                            retryable=name=='query_order',call_id='x',error_code=code)])
            with self.assertRaises(PlanConflict):
                prepare_plan(TaskPlan(tasks=[PlannedTask(next='billing',task_type=task.kind,target_task_id=task.task_id,retry_failed=True)]),
                             {'tasks':[task.model_dump()]},'重试')
        old=ConversationTask(kind='order_query',agent='billing',status='failed')
        with self.assertRaises(PlanConflict):
            prepare_plan(TaskPlan(tasks=[PlannedTask(next='billing',task_type='order_query',retry_failed=True)]),{'tasks':[old.model_dump()]},'再试')

    def test_new_query_keeps_old_partial_task(self):
        old,_=partial_order()
        tasks,ids,_=prepare_plan(TaskPlan(tasks=[PlannedTask(next='billing',task_type='order_query',target_task_id=old.task_id,
            continuation=True,focus=['order'])]),{'tasks':[old.model_dump()]},'现在只查订单状态')
        self.assertEqual(len(tasks),2)
        self.assertEqual(tasks[0].status,'partial')
        self.assertNotEqual(ids[0],old.task_id)

    def test_generic_followup_does_not_inherit_permission_to_submit_a_refund(self):
        from task_state import RouteDecision,prepare_task
        old=ConversationTask(kind='refund_request',agent='billing',status='completed',parameters=TaskParameters(order_id=43659))
        next_task=prepare_task(RouteDecision(next='billing',task_type='general',continuation=True),old.model_dump())
        self.assertEqual(next_task.kind,'general')
        self.assertEqual(next_task.parameters.order_id,43659)

    def test_successful_order_prerequisite_is_usable_when_only_payment_failed(self):
        old,messages=partial_order()
        # Use an actual authorized-query-shaped item list.
        data=json.loads(messages[1+1].content)
        data['items']=[{'product_id':776}]
        messages[2].content=json.dumps(data)
        child=ConversationTask(kind='product_detail',status='awaiting_dependency',depends_on=[old.task_id],dependency_mode='order_products')
        resolved=resolve_dependencies(child,{old.task_id:old},messages)
        self.assertEqual(resolved.status,'ready')
        self.assertEqual(resolved.parameters.product_ids,[776])


class RetryExecutionTest(unittest.TestCase):
    def test_exception_in_one_tool_does_not_drop_other_results(self):
        executed=[]
        @tool('query_order')
        def order(order_id:str)->dict:
            """Fake order."""
            executed.append('order');return {'success':True,'order_id':int(order_id)}
        @tool('check_payment_status')
        def payment(order_id:str)->dict:
            """Fake failed payment."""
            executed.append('payment');raise RuntimeError('PRIVATE_ERROR_BODY')
        @tool('query_invoice')
        def invoice(order_id:str)->dict:
            """Fake invoice."""
            executed.append('invoice');return {'success':True,'order_id':int(order_id)}
        task=ConversationTask(kind='order_query',agent='billing',parameters=TaskParameters(order_id=43659),focus=['payment','invoice'])
        with tool_execution_scope(TurnPolicy(task=task)):
            delta=execute_read_task(task,{t.name:t for t in [order,payment,invoice]}, {})
        result=collect_task_result([HumanMessage(content='查询')]+delta,task)
        self.assertEqual(executed,['order','payment','invoice'])
        self.assertEqual(result.status,'partial')
        self.assertNotIn('PRIVATE_ERROR_BODY',str(delta))

    def test_retry_guard_blocks_successful_or_changed_operations_and_writes(self):
        old,_=partial_order();old.retry_failed=True
        for name,args in [('query_order',{'order_id':'43659'}),('check_payment_status',{'order_id':'43660'}),('request_refund',{'order_id':'43659'})]:
            request=SimpleNamespace(tool_call={'name':name,'args':args,'id':'x'},tool=SimpleNamespace(),runtime=SimpleNamespace(config={}))
            request.tool.get_input_schema=lambda:SimpleNamespace(model_validate=lambda value:SimpleNamespace(model_dump=lambda **kw:value))
            with tool_execution_scope(TurnPolicy(task=old)):
                result=guarded_tool_call(request,lambda req:self.fail('must not execute'))
            self.assertEqual(result.status,'error')


class ContextTest(unittest.TestCase):
    def test_long_context_is_bounded_latest_question_exact_and_history_unchanged(self):
        messages=[]
        for i in range(100):
            messages += [HumanMessage(content=f'old{i}'+'x'*2000),AIMessage(content='answer'+'y'*2000)]
        latest=HumanMessage(content='当前查询：915，数量2，不含税费')
        messages.append(latest)
        before=[m.model_dump() for m in messages]
        bounded,stats=expert_context([SystemMessage(content='system')],messages)
        self.assertLessEqual(message_chars(bounded),16000)
        self.assertEqual(bounded[-1].content,latest.content)
        self.assertGreater(stats['input_chars'],stats['output_chars']*10)
        self.assertEqual([m.model_dump() for m in messages],before)

    def test_compacted_tools_preserve_pairing_chronology_and_numeric_facts(self):
        old_user=HumanMessage(content='old')
        calls=batch([('query_products',{'max_price':100},{'success':True,'matched_count':30,'max_price':100,
            'products':[{'product_id':i,'list_price':'99.9900','description':'原文'*1000} for i in range(30)]})])
        latest=HumanMessage(content='再问一个问题')
        messages=[old_user]+calls+[latest]
        bounded,_=expert_context([SystemMessage(content='system')],messages)
        self.assertLess(bounded.index(next(m for m in bounded if m.type=='tool')),bounded.index(latest))
        ai=next(m for m in bounded if isinstance(m,AIMessage) and m.tool_calls)
        tool_msg=next(m for m in bounded if m.type=='tool')
        self.assertEqual(ai.tool_calls[0]['id'],tool_msg.tool_call_id)
        data=json.loads(tool_msg.content)
        self.assertEqual(data['matched_count'],30)
        self.assertEqual(data['products'][0]['list_price'],'99.9900')
        self.assertEqual(len(json.loads(calls[-1].content)['products']),30)

    def test_orphan_tool_results_are_not_sent_to_model(self):
        messages=[ToolMessage(content='PRIVATE_ORPHAN',tool_call_id='orphan'),HumanMessage(content='now')]
        bounded,_=expert_context([SystemMessage(content='system')],messages)
        self.assertNotIn('PRIVATE_ORPHAN',str(bounded))

    def test_oversized_current_question_is_not_silently_cut(self):
        with self.assertRaises(ContextBudgetError):
            router_context([SystemMessage(content='system')],[HumanMessage(content='x'*25000)])

    def test_task_record_omits_operation_arguments_and_marks_query_excerpt(self):
        task,_=partial_order();task.query='q'*400
        compact=compact_task_record(task)
        self.assertTrue(compact['query_excerpt_only'])
        self.assertNotIn('arguments',str(compact['operations']))


class ReliabilityGraphTest(unittest.TestCase):
    def test_missing_search_presentation_count_is_repaired_before_execution(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from evals.fixtures import CaseFixture
        from langchain_core.exceptions import OutputParserException
        missing=TaskPlan(tasks=[PlannedTask(next='sales',task_type='product_search',query='recommend')])
        valid=TaskPlan(tasks=[PlannedTask(next='sales',task_type='product_search',query='recommend',
            parameters=TaskParameters(limit=1,product_category='all'))])
        with self.assertRaises(OutputParserException):project.validate_planner_counts(missing)
        self.assertIs(project.validate_planner_counts(valid),valid)
        class Expert:
            def invoke(self,state,config):
                return {'messages':state['messages']+[AIMessage(content='{"focus":["products"]}')]}
        with CaseFixture('guest') as f,patch.object(project,'supervisor_router') as router,patch.object(project,'sales_agent',Expert()):
            router.invoke.side_effect=[missing,valid]
            result=invoke_turn(project.app,'recommend one product',f.thread_id)
            self.assertEqual(router.invoke.call_count,2)
            self.assertEqual(result['task']['parameters']['limit'],1)
            self.assertEqual(len(result['task']['displayed_product_ids']),1)
            self.assertEqual(sum(m.type=='tool' for m in result['messages']),1)

    def test_retry_in_a_new_process_reads_the_persisted_operation_ledger(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from evals.fixtures import CaseFixture
        with CaseFixture('owner') as f,patch.object(project,'supervisor_router') as router:
            def fail_once(**kw):raise TimeoutError('temporary payment failure')
            router.invoke.return_value=TaskPlan(tasks=[PlannedTask(next='billing',task_type='order_query',
                parameters=TaskParameters(order_id=f.slots['own_order']),focus=['order','payment','invoice'])])
            with patch.object(project.order_service,'get_payment',fail_once):
                first=invoke_turn(project.app,'查询订单、支付和发票',f.thread_id)
            self.assertEqual(first['task']['status'],'partial')
            code="""
import json,sys
from unittest.mock import patch
import langgraph_supervisor_1 as project
from task_state import TaskPlan,PlannedTask
from chat_runtime import invoke_turn
tid=sys.argv[1]
before=project.app.get_state({'configurable':{'thread_id':tid}}).values
old=before['task']
with patch.object(project,'supervisor_router') as router:
 router.invoke.return_value=TaskPlan(tasks=[PlannedTask(next='billing',task_type='order_query',target_task_id=old['task_id'],retry_failed=True)])
 after=invoke_turn(project.app,'只重试失败部分',tid)
print(json.dumps({'status':after['task']['status'],'tools':[m.name for m in after['messages'][len(before['messages']):] if m.type=='tool'],'operations':len(after['task']['operations'])}))
"""
            result=subprocess.run([sys.executable,'-c',code,f.thread_id],cwd=Path(__file__).resolve().parent.parent,
                                  capture_output=True,text=True,timeout=40,check=True)
            restored=json.loads(result.stdout)
            self.assertEqual(restored,{'status':'completed','tools':['check_payment_status'],'operations':3})

    def test_partial_read_retry_and_operation_state_survive_checkpoint(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from evals.fixtures import CaseFixture
        from conversation_persistence import get_checkpointer
        with CaseFixture('owner') as f,patch.object(project,'supervisor_router') as router:
            actual=project.order_service.get_payment
            count=0
            def flaky(**kwargs):
                nonlocal count
                count+=1
                if count==1:raise TimeoutError('simulated')
                return actual(**kwargs)
            with patch.object(project.order_service,'get_payment',flaky):
                router.invoke.return_value=TaskPlan(tasks=[PlannedTask(next='billing',task_type='order_query',
                    parameters=TaskParameters(order_id=f.slots['own_order']),focus=['order','payment','invoice'])])
                first=invoke_turn(project.app,'查订单、支付和发票',f.thread_id)
                self.assertEqual(first['task']['status'],'partial')
                self.assertIn('已发货',first['messages'][-1].content)
                router.invoke.return_value=TaskPlan(tasks=[PlannedTask(next='billing',task_type='order_query',
                    retry_failed=True,target_task_id=first['task']['task_id'])])
                second=invoke_turn(project.app,'只重试失败部分',f.thread_id)
            self.assertEqual([m.name for m in second['messages'][len(first['messages']):] if m.type=='tool'],['check_payment_status'])
            self.assertEqual(second['task']['status'],'completed')
            self.assertIn('已发货',second['messages'][-1].content)
            saved=get_checkpointer().get_tuple({'configurable':{'thread_id':f.thread_id}})
            self.assertEqual(len(saved.checkpoint['channel_values']['task']['operations']),3)
            self.assertTrue(all(o['status']=='succeeded' for o in saved.checkpoint['channel_values']['task']['operations']))

    def test_bounded_expert_input_does_not_trim_persisted_public_history(self):
        import langgraph_supervisor_1 as project
        from chat_runtime import invoke_turn
        from evals.fixtures import CaseFixture
        captured=[]
        class CaptureExpert:
            def invoke(self,state,config):
                prompt=guarded_prompt('销售助手。最终输出JSON。')(state)
                captured.append(message_chars(prompt))
                return {'messages':state['messages']+[AIMessage(content='{"kind":"data","focus":["products"]}') ]}
        with CaseFixture('guest') as f,patch.object(project,'supervisor_router') as router:
            router.invoke.return_value=TaskPlan(tasks=[PlannedTask(next='sales',task_type='product_detail',parameters=TaskParameters(product_ids=[915]))])
            invoke_turn(project.app,'商品915',f.thread_id)
            padding=[]
            for i in range(60):padding += [HumanMessage(content=f'HISTORY-MARKER-{i} '+'h'*1000),AIMessage(content='neutral '+'a'*1000)]
            project.app.update_state({'configurable':{'thread_id':f.thread_id}},{'messages':padding})
            router.invoke.return_value=TaskPlan(tasks=[PlannedTask(next='sales',task_type='product_search',parameters=TaskParameters(keyword='touring tube',limit=1,product_category='accessories'))])
            with patch.object(project,'sales_agent',CaptureExpert()):
                result=invoke_turn(project.app,'找一款旅行车内胎',f.thread_id)
            self.assertTrue(captured)
            self.assertLessEqual(max(captured),16000)
            self.assertGreater(len(result['messages']),120)
            self.assertTrue(any(m.content.startswith('HISTORY-MARKER-0 ') for m in result['messages'] if m.type=='human'))


class CategoryMetadataTest(unittest.TestCase):
    def test_both_retrieval_sources_respect_catalog_category_and_price(self):
        from adventureworks_service import AdventureWorksService
        service=AdventureWorksService()
        result=service.search_products('适合周末骑行的自行车整车',product_category='bicycles')
        self.assertTrue(result['success'])
        self.assertTrue(result['products'])
        self.assertTrue(result['semantic_available'])
        self.assertTrue(all(p['product_number'].startswith('BK-') for p in result['products']))
        self.assertTrue(all(p['product_subcategory_id'] in [1,2,3] for p in result['products']))
        empty=service.search_products('自行车',max_price=6,product_category='bicycles')
        self.assertEqual(empty['products'],[])
        self.assertFalse(service.search_products('bike',product_category="bicycles OR true")['success'])

    def test_wrong_category_selection_cannot_leak_to_display_or_task_references(self):
        import langgraph_supervisor_1 as project
        rows=[{'product_id':1,'name':'Accessory','product_subcategory_id':10,'list_price':10},
              {'product_id':2,'name':'Bicycle','product_subcategory_id':2,'list_price':100}]
        task=ConversationTask(kind='product_search',agent='sales',parameters=TaskParameters(limit=1,product_category='bicycles'))
        result=TaskResult(product_category='bicycles',current=[ToolEvidence(tool='search_products',data={'success':True,'products':rows})])
        draft,selected=project.limit_product_plan(task,result,'{"focus":["products"],"product_ids":[1]}')
        answer,_=render_business_result(result,draft,'sales')
        saved=complete_task(task,result,selected)
        self.assertEqual(saved.displayed_product_ids,[2])
        self.assertEqual(saved.candidate_product_ids,[2])
        self.assertIn('商品2',answer)
        self.assertNotIn('商品1',answer)

    def test_expert_cannot_remove_the_task_category_from_a_search(self):
        task=ConversationTask(kind='product_search',agent='sales',parameters=TaskParameters(product_category='bicycles'))
        @tool
        def search_products(query:str,product_category:str|None=None)->dict:
            """Test search."""
            return {'success':True}
        request=SimpleNamespace(tool_call={'name':'search_products','args':{'query':'bike'},'id':'drop-category'},
                                tool=search_products,runtime=SimpleNamespace(config={}))
        with tool_execution_scope(TurnPolicy(task=task)),patch('builtins.print'):
            executed=[]
            result=guarded_tool_call(request,lambda req:executed.append(req))
        self.assertEqual(json.loads(result.content)['error_code'],'TASK_TOOL_NOT_ALLOWED')
        self.assertEqual(executed,[])


if __name__=='__main__':
    unittest.main(verbosity=2)
