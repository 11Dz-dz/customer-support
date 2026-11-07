"""Provider wire-format, program evidence and completion-grader regressions."""
import asyncio
from copy import deepcopy
import json
import unittest

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages import messages_from_dict, messages_to_dict
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent, ToolNode

from backend.agent.context_budget import expert_context, message_chars
from backend.agent.deepseek_model import ThinkingChatDeepSeek
from evals.runtime_checks import execution_checks
from backend.tasks.task_execution import seed_product_search
from backend.tasks.task_scheduler import scoped_history
from backend.tasks.task_state import ConversationTask, TaskParameters
from backend.agent.tool_execution import TurnPolicy, guarded_model, guarded_prompt, guarded_tool_call, tool_execution_scope


def pair(*, program=False, reasoning=None):
    extra={'program_execution':True} if program else {}
    if reasoning is not None:extra['reasoning_content']=reasoning
    return [AIMessage(content='',tool_calls=[{'id':'call-one','name':'search_products','args':{'query':'bike'}}],
                      additional_kwargs=extra),
            ToolMessage(content='{"products":[{"product_id":915,"list_price":"39.14"}]}',
                        name='search_products',tool_call_id='call-one')]


class ProviderProjectionTest(unittest.TestCase):
    def test_program_call_is_data_and_original_evidence_is_unchanged(self):
        records=pair(program=True)
        before=deepcopy(messages_to_dict(records))
        view,stats=expert_context([SystemMessage(content='assistant')],[HumanMessage(content='recommend')]+records)
        self.assertFalse(any(isinstance(m,(AIMessage,ToolMessage)) for m in view))
        self.assertIn('39.14',view[-1].content)
        self.assertIn('search_products',view[-1].content)
        self.assertEqual(stats['projected_evidence_groups'],1)
        self.assertEqual(messages_to_dict(records),before)

    def test_legacy_calls_without_reasoning_are_projected(self):
        view,_=expert_context([], [HumanMessage(content='next')]+pair())
        self.assertFalse(any(isinstance(m,ToolMessage) for m in view))
        self.assertIn('915',view[-1].content)

    def test_native_reasoning_survives_persistence_and_context(self):
        records=messages_from_dict(messages_to_dict(pair(reasoning='original-provider-reasoning')))
        view,_=expert_context([], [HumanMessage(content='next')]+records)
        ai=next(m for m in view if isinstance(m,AIMessage))
        self.assertEqual(ai.additional_kwargs['reasoning_content'],'original-provider-reasoning')
        self.assertEqual(ai.tool_calls,records[0].tool_calls)
        self.assertEqual(view[-1].tool_call_id,'call-one')

    def test_program_marker_overrides_a_reasoning_field(self):
        view,_=expert_context([], [HumanMessage(content='next')]+pair(program=True,reasoning='not-model-generated'))
        self.assertFalse(any(isinstance(m,AIMessage) for m in view))
        self.assertNotIn('not-model-generated',str(view))

    def test_filtered_native_call_is_evidence_without_other_task_data(self):
        records=pair(reasoning='native')
        records[0].tool_calls.append({'id':'other','name':'query_order','args':{},'type':'tool_call'})
        records.append(ToolMessage(content='OTHER_TASK_PRIVATE',tool_call_id='other'))
        task=ConversationTask(query='recommend',evidence_call_ids=['call-one'])
        scoped=scoped_history([HumanMessage(content='compound')]+records,task)
        view,_=expert_context([],scoped,task)
        self.assertNotIn('OTHER_TASK_PRIVATE',str(view))
        self.assertFalse(any(isinstance(m,ToolMessage) for m in view))

    def test_program_answer_keeps_chronology_without_fake_assistant_role(self):
        records=[HumanMessage(content='first'),AIMessage(content='previous rendered answer'),HumanMessage(content='now')]
        view,_=expert_context([],records)
        self.assertTrue(all(isinstance(m,HumanMessage) for m in view))
        self.assertIn('previous rendered answer',view[1].content)
        self.assertEqual(view[-1].content,'now')

    def test_oversized_native_reasoning_uses_evidence_without_truncating_source(self):
        records=pair(reasoning='r'*20000)
        view,_=expert_context([], [HumanMessage(content='next')]+records)
        self.assertLessEqual(message_chars(view),16000)
        self.assertIn('39.14',view[-1].content)
        self.assertEqual(len(records[0].additional_kwargs['reasoning_content']),20000)

    def test_payload_passes_real_reasoning_and_never_fills_missing_field(self):
        model=ThinkingChatDeepSeek(model='deepseek-flash',api_key='test-placeholder')
        for reasoning in ['native-reasoning','']:
            with self.subTest(reasoning=reasoning):
                payload=model._get_request_payload(pair(reasoning=reasoning))
                self.assertEqual(payload['messages'][0]['reasoning_content'],reasoning)
        self.assertNotIn('reasoning_content',model._get_request_payload([AIMessage(content='plain')])['messages'][0])

    def test_real_react_http_requests_preserve_reasoning_streaming_and_nonstreaming(self):
        for streaming in (False,True):
            with self.subTest(streaming=streaming):
                payloads=[];executions=[]
                @tool
                def search_products(query:str,min_price:float|None=None,max_price:float|None=None,
                                    product_category:str|None=None,limit:int=5)->dict:
                    """Search test products."""
                    executions.append('search')
                    return {'success':True,'products':[{'product_id':915,'name':'Tube','list_price':'39.14'}]}
                @tool
                def get_product_info(product_name:str)->dict:
                    """Get a product's verified description."""
                    executions.append('detail')
                    return {'success':True,'product':{'product_id':915,'name':'Tube','list_price':'39.14'}}
                def handler(request):
                    body=json.loads(request.content);payloads.append(body)
                    first=len(payloads)==1
                    if first:
                        self.assertFalse(any(m['role'] in {'assistant','tool'} for m in body['messages']))
                        self.assertIn('39.14',str(body['messages']))
                    else:
                        ai=[m for m in body['messages'] if m['role']=='assistant']
                        self.assertEqual(len(ai),1)
                        self.assertEqual(ai[0]['reasoning_content'],'wire-reasoning')
                        self.assertEqual(ai[0]['tool_calls'][0]['id'],'native-call')
                        self.assertEqual([m['tool_call_id'] for m in body['messages'] if m['role']=='tool'],['native-call'])
                    call={'id':'native-call','type':'function','function':{'name':'get_product_info','arguments':'{"product_name":"915"}'}}
                    final='{"kind":"data","focus":["products"],"product_ids":[915]}'
                    if body.get('stream'):
                        deltas=[{'role':'assistant','reasoning_content':'wire-reasoning'}]
                        deltas.append({'tool_calls':[{'index':0,**call}]} if first else {'content':final})
                        deltas.append({})
                        chunks=[{'id':'test','object':'chat.completion.chunk','created':0,'model':'deepseek-flash',
                                 'choices':[{'index':0,'delta':d,'finish_reason':('tool_calls' if first else 'stop') if i==2 else None}]}
                                for i,d in enumerate(deltas)]
                        text=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n'
                        return httpx.Response(200,headers={'content-type':'text/event-stream'},text=text)
                    message={'role':'assistant','content':'' if first else final,'reasoning_content':'wire-reasoning'}
                    if first:message['tool_calls']=[call]
                    return httpx.Response(200,json={'id':'test','object':'chat.completion','created':0,'model':'deepseek-flash',
                        'choices':[{'index':0,'message':message,'finish_reason':'tool_calls' if first else 'stop'}]})
                sync=httpx.Client(transport=httpx.MockTransport(handler))
                async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
                model=ThinkingChatDeepSeek(model='deepseek-flash',api_key='test-placeholder',streaming=streaming,
                                         http_client=sync,http_async_client=async_client,max_retries=0)
                tools=[search_products,get_product_info]
                agent=create_react_agent(model=guarded_model(model,tools),tools=ToolNode(tools,wrap_tool_call=guarded_tool_call),
                                         prompt=guarded_prompt('Return a JSON answer plan.'),checkpointer=False)
                task=ConversationTask(kind='product_search',agent='sales',parameters=TaskParameters(limit=1))
                try:
                    with tool_execution_scope(TurnPolicy(task=task)):
                        seed=seed_product_search(task,'recommend', {t.name:t for t in tools},{})
                        result=agent.invoke({'messages':[HumanMessage(content='recommend')]+seed})
                    self.assertEqual(executions,['search','detail'])
                    self.assertEqual(len(payloads),2)
                    self.assertIn('product_ids',result['messages'][-1].content)
                finally:
                    sync.close();asyncio.run(async_client.aclose())


class ExecutionGraderTest(unittest.TestCase):
    def test_safe_product_fallback_is_not_success(self):
        trace={'models':[{'role':'sales','success':False,'provider_status_code':400}],
               'task_execution':[{'task_id':'one','kind':'product_search','agent':'sales','status':'partial','failure_code':'EXECUTION_FAILED'}]}
        checks=execution_checks(trace)
        self.assertFalse(all(c['passed'] for c in checks))
        self.assertFalse(next(c for c in checks if c['name']=='recommendation_completed')['passed'])

    def test_completed_recommendation_requires_a_successful_specialist(self):
        trace={'models':[], 'task_execution':[{'task_id':'one','kind':'product_search','agent':'sales','status':'completed','failure_code':None}]}
        self.assertFalse(all(c['passed'] for c in execution_checks(trace)))
        trace['models']=[{'role':'sales','success':True}]
        self.assertTrue(all(c['passed'] for c in execution_checks(trace)))

    def test_controlled_partial_failure_is_allowed_but_not_provider_400(self):
        trace={'models':[], 'task_execution':[{'task_id':'one','kind':'order_query','agent':'billing','status':'partial','failure_code':'MODEL_TIMEOUT'}]}
        self.assertTrue(all(c['passed'] for c in execution_checks(trace,{'statuses':{'partial':1}})))
        self.assertFalse(all(c['passed'] for c in execution_checks(trace)))
        trace['models']=[{'role':'billing','success':False,'provider_status_code':400}]
        self.assertFalse(all(c['passed'] for c in execution_checks(trace,{'statuses':{'partial':1}})))


if __name__=='__main__':unittest.main(verbosity=2)
