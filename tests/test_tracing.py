"""Real LangSmith serialization over a recording transport; never send test traces to SaaS."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import requests
from langchain_core.messages import AIMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import StateGraph,MessagesState

import langsmith_tracing as tracing
from chat_events import emit_event
from chat_runtime import invoke_turn,stream_turn,turn_config
from request_deadline import invoke_model


class RecordingSession(requests.Session):
    def __init__(self):super().__init__();self.records=[]

    def request(self,method,url,**kwargs):
        body=kwargs.get('data')
        if isinstance(body,bytes):body=body.decode()
        payload=kwargs.get('json') if body is None else json.loads(body)
        self.records.append({'method':method.upper(),'url':url,'payload':payload})
        response=requests.Response();response.status_code=200;response._content=b'{}'
        response.headers['content-type']='application/json';response.url=url
        response.request=requests.Request(method,url).prepare()
        return response


class TracingTests(unittest.TestCase):
    def setUp(self):
        self.environment=patch.dict(os.environ,{'LANGSMITH_TRACING':'true','LANGCHAIN_TRACING_V2':'false',
            'LANGSMITH_API_KEY':'lsv2_pt_testcredential0123456789','LANGSMITH_ENDPOINT':'https://trace.test',
            'LANGSMITH_PROJECT':'test-customer-support','LANGSMITH_WORKSPACE_ID':'',
            'LANGSMITH_AGENT_ID':'','LANGSMITH_HIDE_INPUTS':'false','LANGSMITH_HIDE_OUTPUTS':'false'})
        self.environment.start();self.transport=RecordingSession()
        self.client=tracing.make_client(tracing.settings(),session=self.transport,auto_batch_tracing=False,
            info={'batch_ingest_config':{'use_multipart_endpoint':False}})
        self.client_patch=patch.object(tracing,'get_client',return_value=self.client)
        self.client_patch.start()

    def tearDown(self):
        self.client_patch.stop();self.client.close(timeout=2);self.environment.stop()

    def wait_for_root_end(self):
        for _ in range(100):
            if any(r['method']=='PATCH' and r['payload'].get('name')=='customer_support.turn'
                   for r in self.transport.records):return
            time.sleep(.01)
        self.fail('LangSmith root end was not serialized')

    def creates(self):
        return [r['payload'] for r in self.transport.records if r['method']=='POST' and r['url'].endswith('/runs')]

    def test_disabled_and_missing_key_never_create_client_or_default_tracer(self):
        for env in [{'LANGSMITH_TRACING':'false'},{'LANGSMITH_API_KEY':''}]:
            with patch.dict(os.environ,env),patch.object(tracing,'get_client') as client:
                config=turn_config('trace-test-'+uuid4().hex)
                with tracing.trace_turn(config,'hello','invoke') as handle:
                    self.assertIsNone(handle.root)
                client.assert_not_called();self.assertNotIn('langsmith_trace_id',config['metadata'])
        self.assertFalse(self.transport.records)

    def test_setup_failure_does_not_run_business_twice_or_mask_business_error(self):
        config=turn_config('trace-test-'+uuid4().hex);calls=[]
        with patch.object(tracing,'get_client',side_effect=RuntimeError('private credentials')):
            with self.assertRaisesRegex(ValueError,'business exception'):
                with tracing.trace_turn(config,'hello','invoke'):
                    calls.append(1);raise ValueError('business exception')
        self.assertEqual(calls,[1]);self.assertFalse(self.transport.records)

    def test_redaction_is_non_mutating_preserves_usage_and_masks_nested_secrets(self):
        original={'messages':[AIMessage(content='sk-credential0123456789',
            additional_kwargs={'reasoning_content':'private reasoning'},
            usage_metadata={'input_tokens':7,'output_tokens':3,'total_tokens':10})],
            'password':'sensitive','headers':{'Authorization':'Bearer token'},'api_key':'sensitive',
            'database_url':'postgresql://fake:db-private-password@host/db',
            'metadata':{'request_id':'keep','thread_id':'keep','token_usage':{'total_tokens':10}}}
        before=deepcopy(original)
        with patch.dict(os.environ,{'DATABASE_URL':original['database_url']}):result=tracing.redact_trace_data(original)
        text=json.dumps(result)
        for value in ['sensitive','private reasoning','sk-credential0123456789','db-private-password']:
            self.assertNotIn(value,text)
        self.assertEqual(result['metadata']['token_usage']['total_tokens'],10)
        self.assertEqual(original,before)

    def graph(self):
        model=FakeMessagesListChatModel(responses=[AIMessage(content='checked',
            additional_kwargs={'reasoning_content':'private reasoning'},
            usage_metadata={'input_tokens':7,'output_tokens':3,'total_tokens':10})])
        @tool
        def traced_lookup(query:str)->dict:
            """Return test data; no database/business write."""
            return {'success':True,'query':query}
        def node(state:MessagesState,config:RunnableConfig):
            emit_event('route',config,agent='billing',task_id='test-task')
            # Uses the same async model loop as the actual project.
            invoke_model(model,state['messages'],config)
            result=traced_lookup.invoke({'query':'test-only'},config)
            emit_event('answer_verified',config,plan_fallback=True,mode='test-evidence')
            return {'messages':[AIMessage(content='verified test answer')]}
        graph=StateGraph(MessagesState);graph.add_node('test_expert',node);graph.set_entry_point('test_expert');graph.set_finish_point('test_expert')
        return graph.compile()

    def test_real_sdk_links_graph_node_async_model_tool_and_events_under_one_root(self):
        result=invoke_turn(self.graph(),'test input', 'trace-test-'+uuid4().hex)
        self.assertEqual(result['messages'][-1].content,'verified test answer')
        self.wait_for_root_end()
        records=self.creates();names={r['name'] for r in records}
        self.assertTrue({'customer_support.turn','customer_support.graph','test_expert','traced_lookup'}<=names,names)
        root=next(r for r in records if r['name']=='customer_support.turn')
        self.assertTrue(any(r['run_type']=='llm' for r in records))
        self.assertEqual({r['trace_id'] for r in records},{root['id']})
        self.assertEqual(root['extra']['metadata']['request_id'],str(root['id']).replace('-',''))
        self.assertTrue(all(r.get('parent_run_id') for r in records if r['id']!=root['id']))
        payloads=json.dumps(self.transport.records)
        self.assertIn('answer_verified',payloads);self.assertIn('plan_fallback',payloads)
        self.assertNotIn('private reasoning',payloads)
        self.assertIn('total_tokens',payloads)
        end=[r['payload'] for r in self.transport.records if r['method']=='PATCH' and r['url'].endswith(root['id'])][-1]
        self.assertEqual(end['outputs']['answer'],'verified test answer')
        self.assertIsNotNone(end['end_time'])

    def test_stream_and_invoke_both_return_same_public_answer(self):
        parts=list(stream_turn(self.graph(),'hello','trace-test-'+uuid4().hex))
        self.assertEqual(''.join(p['text'] for p in parts if p['type']=='text'),'verified test answer')
        self.assertEqual(len([r for r in self.creates() if r['name']=='customer_support.turn']),1)

    def test_concurrent_threads_and_metadata_remain_isolated(self):
        def run(index):
            config=turn_config('trace-test-user-'+str(index)+'-'+uuid4().hex)
            with tracing.request_metadata_scope(web_run_id='web-'+str(index),entrypoint='web',
                    eval_batch_id='batch-'+str(index),eval_case_id='N01',eval_turn=1):
                with tracing.trace_turn(config,'message-'+str(index),'stream') as handle:
                    emit_event('task_end',config,task_id='task-'+str(index),status='completed',task_type='order_query')
                    handle.finish('answer-'+str(index))
            self.assertEqual(tracing.request_metadata(),{})
        with ThreadPoolExecutor(2) as pool:list(pool.map(run,[1,2]))
        roots=[r for r in self.creates() if r['name']=='customer_support.turn']
        self.assertEqual(len(roots),2);self.assertEqual(len({r['id'] for r in roots}),2)
        for root in roots:
            index=root['inputs']['message'].split('-')[-1]
            self.assertEqual(root['extra']['metadata']['web_run_id'],'web-'+index)
            self.assertEqual(root['extra']['metadata']['eval_batch_id'],'batch-'+index)
            self.assertEqual(root['extra']['metadata']['eval_case_id'],'N01')
            self.assertEqual(root['extra']['metadata']['eval_turn'],'1')

    def test_metadata_only_mode_hides_inputs_outputs_but_retains_routing_metadata(self):
        with patch.dict(os.environ,{'LANGSMITH_HIDE_INPUTS':'true','LANGSMITH_HIDE_OUTPUTS':'true'}):
            client=tracing.make_client(tracing.settings(),session=self.transport,auto_batch_tracing=False,
                info={'batch_ingest_config':{'use_multipart_endpoint':False}})
            try:
                with patch.object(tracing,'get_client',return_value=client):
                    config=turn_config('trace-test-'+uuid4().hex)
                    with tracing.trace_turn(config,'DO_NOT_UPLOAD','invoke') as handle:handle.finish('DO_NOT_UPLOAD')
                    self.wait_for_root_end()
            finally:client.close(timeout=2)
        payloads=json.dumps(self.transport.records)
        self.assertNotIn('DO_NOT_UPLOAD',payloads);self.assertIn('request_id',payloads)

    def test_root_error_retains_safe_type_and_does_not_suppress_exception(self):
        config=turn_config('trace-test-'+uuid4().hex)
        with self.assertRaises(RuntimeError):
            with tracing.trace_turn(config,'test','invoke'):raise RuntimeError('DO_NOT_UPLOAD_ERROR_BODY')
        self.wait_for_root_end()
        updates=[r['payload'] for r in self.transport.records if r['method']=='PATCH']
        self.assertEqual(updates[-1]['error'],'RuntimeError')
        self.assertNotIn('DO_NOT_UPLOAD_ERROR_BODY',json.dumps(self.transport.records))


if __name__=='__main__':unittest.main()
