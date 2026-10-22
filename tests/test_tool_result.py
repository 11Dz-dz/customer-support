"""Tool boundary, failure taxonomy and downstream behavior; no live model calls."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from tool_result import (ToolResult,business_payload,exception_result,failure,normalize_result,
                         result_boundary,validate_business_data)
from task_result import collect_task_result
from task_state import ConversationTask,TaskParameters,complete_task
from task_operations import retry_operations
from answer_contract import render_business_result


def result_messages(name,result):
    return [HumanMessage(content='test'),AIMessage(content='',tool_calls=[{'name':name,'args':{'product_name':'999999'},'id':'c'}]),
        ToolMessage(name=name,tool_call_id='c',content=result.model_dump_json())]


class ToolContractTest(unittest.TestCase):
    def test_business_state_is_not_execution_state(self):
        result=normalize_result({'success':True,'status':'pending','refund_id':'R','order_id':1},'request_refund')
        self.assertEqual(result.status,'success')
        self.assertEqual(result.data['status'],'pending')
        self.assertEqual(business_payload(result)['status'],'pending')

    def test_payment_and_invoice_unknown_are_successful_facts(self):
        for name,field in [('check_payment_status','payment_status'),('query_invoice','invoice_status')]:
            with self.subTest(name=name):
                r=normalize_result({'success':True,'order_id':1,field:'unknown'},name)
                self.assertEqual(r.status,'success');self.assertFalse(r.retryable)

    def test_empty_search_is_not_an_exception(self):
        r=normalize_result({'success':True,'products':[],'returned_count':0},'search_products')
        self.assertEqual(r.status,'empty');self.assertFalse(r.retryable)

    def test_product_not_found_ends_empty_without_a_retry(self):
        r=failure('PRODUCT_NOT_FOUND')
        task=ConversationTask(kind='product_detail',agent='sales',parameters=TaskParameters(product_ids=[999999]))
        result=collect_task_result(result_messages('get_product_info',r),task)
        saved=complete_task(task,result)
        answer,_=render_business_result(result,'{}','sales')
        self.assertEqual(result.status,'empty');self.assertEqual(retry_operations(saved),[])
        self.assertIn('没有找到',answer)

    def test_invalid_quantity_does_not_offer_same_argument_retry(self):
        r=failure('INVALID_QUANTITY')
        answer,_=render_business_result(collect_task_result(result_messages('calculate_price',r)),'{}','sales')
        self.assertIn('修改数量',answer);self.assertFalse(r.retryable)

    def test_unknown_failure_is_not_guessed_from_its_text(self):
        for text in ['timeout','数据库暂时连接失败','RATE_LIMITED','PRIVATE_KEY=secret']:
            r=normalize_result({'success':False,'error':text},'query_order')
            self.assertEqual(r.error_code,'UNCLASSIFIED_TOOL_ERROR');self.assertFalse(r.retryable)
            self.assertNotIn(text,r.model_dump_json())

    def test_exception_mapping_uses_types_and_hides_exception_bodies(self):
        for exc,status in [(TimeoutError('SECRET'),'temporary_error'),(ConnectionError('SECRET'),'temporary_error'),
                           (RuntimeError('SECRET'),'error'),(ValueError('SECRET'),'error')]:
            r=exception_result(exc);self.assertEqual(r.status,status);self.assertNotIn('SECRET',r.model_dump_json())

    def test_database_auth_errors_are_not_treated_as_network_failures(self):
        import psycopg
        self.assertFalse(exception_result(psycopg.errors.InvalidPassword('SECRET')).retryable)
        self.assertTrue(exception_result(psycopg.OperationalError('SECRET')).retryable)
        self.assertEqual(exception_result(psycopg.errors.QueryCanceled('SECRET')).error_code,'DATABASE_TIMEOUT')

    def test_incoherent_or_unknown_versions_are_rejected(self):
        valid=failure('INVALID_QUANTITY').model_dump()
        for delta in [{'retryable':True},{'schema_version':2},{'schema_version':True},
                      {'status':'success'},{'extra':'SECRET'},{'data':[]},{'message':''}]:
            parsed=normalize_result({**valid,**delta})
            self.assertEqual(parsed.error_code,'MALFORMED_TOOL_RESULT');self.assertFalse(parsed.retryable)

    def test_missing_success_flag_or_unexpected_plain_text_is_malformed(self):
        for value in [{},[],None,{'success':'true'},'PRIVATE_ERROR_BODY']:
            self.assertEqual(normalize_result(value,'query_order').error_code,'MALFORMED_TOOL_RESULT')

    def test_transport_error_overrides_a_successful_body(self):
        r=normalize_result({'success':True,'order_id':1},'query_order',transport_error=True)
        self.assertFalse(r.ok);self.assertEqual(r.data,{})

    def test_fresh_order_boundary_checks_minimum_factual_fields(self):
        @result_boundary
        def query_order():return {'success':True,'order_id':1}
        self.assertEqual(query_order()['error_code'],'MALFORMED_TOOL_RESULT')

    def test_old_legacy_evidence_is_read_without_mutating_messages(self):
        m=ToolMessage(name='query_order',tool_call_id='legacy',content='{"success":true,"order_id":1,"status":"已发货"}')
        original=m.content
        result=collect_task_result([HumanMessage(content='order'),m])
        self.assertEqual(result.current[0].data['status'],'已发货');self.assertEqual(m.content,original)
        from evaluate_customer_queries import tool_results
        from evaluate_session_identity import results
        envelope=normalize_result(m.content,'query_order')
        new=ToolMessage(name='query_order',tool_call_id='new',content=envelope.model_dump_json())
        a=tool_results([m,new]);b=results([m,new])
        self.assertEqual(a[0][1],a[1][1]);self.assertEqual(b[0],b[1])
        denied=ToolMessage(name='query_order',tool_call_id='denied',content=failure('ORDER_NOT_ACCESSIBLE').model_dump_json())
        self.assertFalse(results([denied])[0]['success'])

    def test_write_failures_are_not_added_to_read_retry_operations(self):
        task=ConversationTask(kind='refund_request',agent='billing',parameters=TaskParameters(order_id=1))
        saved=complete_task(task,collect_task_result(result_messages('request_refund',failure('TOOL_TIMEOUT')),task))
        self.assertEqual(retry_operations(saved),[])
        @result_boundary
        def request_refund():raise TimeoutError('secret')
        self.assertFalse(request_refund()['retryable'])

    def test_agent_cannot_repeat_a_failed_write_within_the_same_task(self):
        from langchain_core.tools import tool
        from tool_execution import TurnPolicy,tool_execution_scope,guarded_tool_call
        @tool
        def request_refund(order_id:str)->dict:
            """Test application, no database write."""
            return failure('TOOL_TIMEOUT').model_dump()
        task=ConversationTask(kind='refund_request',agent='billing')
        calls=[]
        with tool_execution_scope(TurnPolicy(task=task)):
            for i in range(2):
                request=SimpleNamespace(tool=request_refund,runtime=SimpleNamespace(config={}),
                    tool_call={'name':'request_refund','args':{'order_id':'1'},'id':str(i)})
                def execute(req):
                    calls.append(req.tool_call['id'])
                    return ToolMessage(content=failure('TOOL_TIMEOUT').model_dump_json(),name='request_refund',tool_call_id=str(i))
                result=guarded_tool_call(request,execute)
        self.assertEqual(calls,['0'])
        self.assertEqual(normalize_result(result.content).error_code,'WRITE_RETRY_NOT_ALLOWED')

    def test_temporary_read_failure_can_be_retried_with_the_recorded_args(self):
        task=ConversationTask(kind='product_detail',agent='sales')
        saved=complete_task(task,collect_task_result(result_messages('get_product_info',failure('TOOL_TIMEOUT')),task))
        self.assertEqual(retry_operations(saved),[('get_product_info',{'product_name':'999999'})])

    def test_failed_order_payload_cannot_escape_the_denial(self):
        r=normalize_result({'success':False,'error_code':'ORDER_NOT_ACCESSIBLE','order_id':42,'customer_id':7,'total_due':'123'})
        self.assertEqual(r.status,'forbidden');self.assertEqual(r.data,{})

    def test_healthy_probe_state_survives_the_envelope(self):
        r=normalize_result({'success':True,'component':'server','status':'degraded','metrics':{}},'check_system_status')
        self.assertEqual(r.status,'success');self.assertEqual(r.data['status'],'degraded')

    def test_production_tools_return_the_same_versioned_shape(self):
        import langgraph_supervisor_1 as project
        for code in ['500','HTTP 500','http:500',' 500 ']:
            r=ToolResult.model_validate(project.search_error_code.invoke({'error_code':code}))
            self.assertEqual(r.status,'success');self.assertIn('内部服务器错误',r.data['text'])
        calls=[(project.search_error_code,{'error_code':'404'}),(project.search_error_code,{'error_code':'UNKNOWN'}),
               (project.get_product_info,{'product_name':'2147483000'}),(project.calculate_price,{'product':'915','quantity':-1}),
               (project.search_products,{'query':'zz_no_product_exists'}),(project.query_order,{'order_id':'43659'})]
        for tool,args in calls:
            with self.subTest(tool=tool.name):
                r=ToolResult.model_validate(tool.invoke(args))
                self.assertEqual(r.schema_version,1)
                self.assertEqual(set(r.model_dump()),{'schema_version','status','error_code','message','data','retryable'})


if __name__=='__main__':unittest.main()
