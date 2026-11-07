"""Durable acceptance and graph checkpoints on isolated PostgreSQL web tables; fake models."""
import json
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage,HumanMessage,ToolMessage
from backend.conversation.turn_inbox import merge_inflight_plan
from backend.tasks.task_state import ConversationTask,TaskParameters,TaskPlan,PlannedTask
from tests import test_web as web_tests


class InboxWebTests(unittest.TestCase):
    setUp=web_tests.WebTests.setUp
    tearDown=web_tests.WebTests.tearDown
    login=web_tests.WebTests.login
    conversation=web_tests.WebTests.conversation
    send=web_tests.WebTests.send
    await_run=web_tests.WebTests.await_run

    def test_busy_input_is_durable_idempotent_and_bounded(self):
        self.gate.clear();cid=self.conversation();first=self.send(cid).json()
        request=str(uuid4());second=self.send(cid,'预算500以内',request).json()
        repeated=self.send(cid,'预算500以内',request).json()
        self.assertEqual(first['run_id'],second['run_id'])
        self.assertEqual(second['message_id'],repeated['message_id'])
        for i in range(6):self.assertEqual(self.send(cid,'extra '+str(i)).status_code,202)
        rejected=self.send(cid,'overflow')
        self.assertEqual(rejected.status_code,429);self.assertEqual(rejected.json()['code'],'INPUT_QUEUE_FULL')
        rows=self.store.input_history(cid);self.assertEqual(len(rows),8)
        self.assertEqual(sum(r['message']=='预算500以内' for r in rows),1)

    def test_final_seal_defers_late_input_and_followup_is_fifo_barrier(self):
        cid=self.conversation()
        root,_=self.store.reserve_run(self.a['user_id'],cid,uuid4(),'first',2)
        batch=uuid4();self.store.claim_input(root['run_id'],root['input_id'],batch)
        self.assertEqual(self.store.poll_inputs(root['run_id'],batch,seal=True),([],False))
        late,_=self.store.reserve_run(self.a['user_id'],cid,uuid4(),'late condition',2)
        self.assertEqual(late['mode'],'followup')
        next_input=self.store.advance_or_finish(root['run_id'],batch)
        self.assertEqual(next_input['input_id'],late['input_id'])
        # A deliberately queued independent question cannot be overtaken by a later steer.
        self.store.claim_input(root['run_id'],late['input_id'],uuid4())
        queued,_=self.store.reserve_run(self.a['user_id'],cid,uuid4(),'next question',2,'followup')
        later,_=self.store.reserve_run(self.a['user_id'],cid,uuid4(),'later addition',2)
        self.assertEqual(later['mode'],'followup')
        self.assertEqual(self.store.poll_inputs(root['run_id'],uuid4()),([],False))
        with self.store.connect() as c:c.execute("UPDATE web_runs SET status='interrupted' WHERE run_id=%s",(root['run_id'],))

    def test_accepted_input_is_visible_before_graph_persists_it_and_owned(self):
        self.gate.clear();cid=self.conversation();self.send(cid,'first')
        self.send(cid,'预算500以内')
        history=self.client.get(f'/api/conversations/{cid}/messages').json()
        self.assertEqual([m['content'] for m in history['messages']],['first','预算500以内'])
        self.auth=self.login('bob','test-password-bobby')
        self.assertEqual(self.client.post(f'/api/conversations/{cid}/stop',headers=self.auth).status_code,404)
        self.assertEqual(self.send(cid,'unauthorized').status_code,404)

    def test_refresh_during_first_identity_checkpoint_is_not_legacy_history(self):
        from types import SimpleNamespace
        cid=self.conversation();root,_=self.store.reserve_run(self.a['user_id'],cid,uuid4(),'initial',2)
        self.runtime.graph=SimpleNamespace(get_state=lambda config:SimpleNamespace(values={
            'messages':[HumanMessage(content='initial',id=str(root['input_id']))]}))
        response=self.client.get(f'/api/conversations/{cid}/messages')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(response.json()['messages']),1)
        self.store.finish(root['run_id'],'interrupted','error',{'message':'test cleanup'})

    def test_late_message_runs_after_first_answer_in_same_serial_worker(self):
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        calls=[]
        def stream(graph,message,thread):
            calls.append((message,thread))
            yield {'type':'text','text':'first answer' if len(calls)==1 else 'second answer'}
            if len(calls)==1:entered.set();release.wait(10)
        self.runtime.stream=stream
        cid=self.conversation();first=self.send(cid,'first question').json()
        self.assertTrue(entered.wait(8))
        second=self.send(cid,'another question').json()
        self.assertEqual(second['mode'],'followup');self.assertEqual(second['run_id'],first['run_id'])
        release.set();self.await_run(first['run_id'])
        self.assertEqual([m for m,t in calls],['first question','another question'])
        self.assertEqual(calls[0][1],calls[1][1])
        texts=[e['payload'] for e in self.store.events(first['run_id'],0) if e['event_type']=='text']
        self.assertEqual([e['text'] for e in texts],['first answer','second answer'])
        self.assertNotEqual(texts[0]['message_id'],texts[1]['message_id'])

    def test_restart_preserves_unprocessed_inputs_without_replaying_work(self):
        from webapp.runtime import WebRuntime
        cid=self.conversation()
        first,_=self.store.reserve_run(self.a['user_id'],cid,uuid4(),'before restart',2)
        self.store.reserve_run(self.a['user_id'],cid,uuid4(),'budget 500',2)
        self.runtime.close()
        replacement=WebRuntime(self.store,stream=self.runtime.stream,graph=self.runtime.graph,
                               lease_key=self.runtime.lease_key)
        replacement.start()
        try:
            self.assertEqual(self.store.run(self.a['user_id'],first['run_id'])['status'],'interrupted')
            self.assertEqual([r['message'] for r in self.store.input_history(cid)],['before restart','budget 500'])
            self.assertTrue(all(r['phase']=='interrupted' for r in self.store.input_history(cid)))
            self.assertEqual(self.calls,[])
        finally:replacement.close()

    def test_seal_and_new_input_race_never_loses_an_accepted_message(self):
        from concurrent.futures import ThreadPoolExecutor
        cid=self.conversation();root,_=self.store.reserve_run(self.a['user_id'],cid,uuid4(),'initial',2)
        batch=uuid4();self.store.claim_input(root['run_id'],root['input_id'],batch)
        barrier=threading.Barrier(2)
        def seal():
            barrier.wait();return self.store.poll_inputs(root['run_id'],batch,seal=True)[0]
        def accept():
            barrier.wait();return self.store.reserve_run(self.a['user_id'],cid,uuid4(),'new budget',2)[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(seal);b=pool.submit(accept);consumed=a.result();accepted=b.result()
        row=next(r for r in self.store.input_history(cid) if r['input_id']==accepted['input_id'])
        if consumed:
            self.assertEqual(row['phase'],'included');self.assertEqual(row['batch_id'],batch)
        else:
            self.assertEqual(row['mode'],'followup');self.assertEqual(row['phase'],'pending')
        self.store.finish(root['run_id'],'interrupted','error',{'message':'test cleanup'})

    def graph_models(self,phase='expert'):
        import backend.agent.langgraph_supervisor_1 as project
        from backend.conversation.chat_runtime import stream_turn
        self.runtime.stream=stream_turn;self.runtime.graph=project.app
        entered,release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        searches=[];expert_calls=[];router_calls=[]
        def router(messages,config):
            number=len(router_calls);router_calls.append(messages)
            if number==0 and phase=='planner':entered.set();release.wait(10)
            return TaskPlan(tasks=[PlannedTask(next='sales',task_type='product_search',
                continuation=number>0,query='推荐五款自行车'+('，预算500以内' if number else ''),
                parameters=TaskParameters(keyword='bicycle',product_category='bicycles',limit=5,
                    max_price=500 if number else None))])
        def seed(task,query,tools,config):
            searches.append(task.parameters.max_price)
            pid,price=(2,400) if task.parameters.max_price==500 else (1,700)
            call='fake-search-'+uuid4().hex
            return [AIMessage(content='',tool_calls=[{'name':'search_products','args':{'query':'bicycle'},'id':call}],
                              additional_kwargs={'program_execution':True}),
                ToolMessage(name='search_products',tool_call_id=call,content=json.dumps({'success':True,
                    'products':[{'product_id':pid,'product_model_id':pid,'product_subcategory_id':1,
                                 'name':'Test bicycle','list_price':price,'description_zh':'测试自行车'}]}))]
        class Expert:
            def invoke(inner,state,config):
                number=len(expert_calls);expert_calls.append(state)
                if number==0 and phase=='expert':entered.set();release.wait(10)
                rows=[json.loads(m.content)['products'] for m in state['messages'] if isinstance(m,ToolMessage)]
                pid=rows[-1][0]['product_id']
                return {'messages':state['messages']+[AIMessage(content=json.dumps({'kind':'data','focus':['products'],'product_ids':[pid]}))]}
        return project,router,seed,Expert(),entered,release,searches,router_calls

    def exercise_budget_update(self,phase):
        project,router,seed,expert,entered,release,searches,routers=self.graph_models(phase)
        cid=self.conversation()
        with patch.object(project,'supervisor_router') as model,patch.object(project,'seed_product_search',seed),patch.object(project,'sales_agent',expert):
            model.invoke.side_effect=router
            first=self.send(cid,'推荐一款自行车。').json()
            self.assertTrue(entered.wait(8),'graph did not reach the controlled checkpoint')
            added=self.send(cid,'预算500以内。').json();self.assertEqual(added['run_id'],first['run_id'])
            release.set();run=self.await_run(first['run_id']);self.assertEqual(run['status'],'completed')
        events=self.store.events(first['run_id'],0)
        answer=''.join(e['payload'].get('text','') for e in events if e['event_type']=='text')
        self.assertIn('400',answer);self.assertNotIn('700',answer)
        self.assertEqual(searches,[500] if phase=='planner' else [None,500])
        history=self.client.get(f'/api/conversations/{cid}/messages').json()['messages']
        self.assertEqual(sum(m['role']=='assistant' for m in history),1)
        self.assertEqual([m['content'] for m in history if m['role']=='user'],['推荐一款自行车。','预算500以内。'])
        self.assertTrue(all(r['phase']=='answered' for r in self.store.input_history(cid)))

    def test_budget_arriving_during_expert_replaces_unpublished_old_answer(self):
        self.exercise_budget_update('expert')

    def test_budget_arriving_during_planning_is_applied_before_first_search(self):
        self.exercise_budget_update('planner')

    def test_stop_waits_for_current_work_and_does_not_publish_old_recommendation(self):
        project,router,seed,expert,entered,release,searches,routers=self.graph_models()
        cid=self.conversation()
        with patch.object(project,'supervisor_router') as model,patch.object(project,'seed_product_search',seed),patch.object(project,'sales_agent',expert):
            model.invoke.side_effect=router;first=self.send(cid,'推荐一款自行车').json()
            self.assertTrue(entered.wait(8));self.send(cid,'预算500以内')
            stop=self.client.post(f'/api/conversations/{cid}/stop',headers=self.auth)
            self.assertEqual(stop.status_code,200);self.assertTrue(stop.json()['requested'])
            release.set();self.assertEqual(self.await_run(first['run_id'])['status'],'interrupted')
        events=self.store.events(first['run_id'],0)
        self.assertEqual(events[-1]['payload']['code'],'RUN_STOPPED')
        self.assertNotIn('700',''.join(e['payload'].get('text','') for e in events))
        self.assertEqual(searches,[None]);self.assertEqual(self.store.input_history(cid)[1]['phase'],'cancelled')


class InflightPlanTests(unittest.TestCase):
    def test_handoff_write_only_starts_after_final_input_boundary(self):
        import backend.agent.langgraph_supervisor_1 as project
        from backend.conversation.turn_inbox import inbox_scope
        task=ConversationTask(kind='human_handoff',agent='billing')
        class Inbox:
            sealed=False
            def poll(self,*,seal=False):
                if seal:self.sealed=True
                return []
        inbox=Inbox()
        def handoff(*args):
            self.assertTrue(inbox.sealed)
            return {'ticket_id':'test'}
        with inbox_scope(inbox),patch.object(project,'create_handoff',handoff), \
                patch.object(project,'ticket_text',return_value='待人工处理'), \
                patch('langgraph.config.get_stream_writer',return_value=lambda value:None):
            result=project.answer_node({'tasks':[task.model_dump()], 'turn_task_ids':[task.task_id],
                'messages':[HumanMessage(content='转人工')],'turn_outputs':[]}, {})
        self.assertIn('待人工处理',result['messages'][-1].content)

    def test_preserves_unaffected_tasks_but_discards_changed_revision_answer(self):
        order=ConversationTask(kind='order_query',status='completed')
        search=ConversationTask(kind='product_search',status='ready',revision=2)
        state={'inflight_replan':True,'turn_task_ids':[order.task_id,search.task_id]}
        outputs=[{'task_id':order.task_id,'revision':1,'content':'verified order'},
                 {'task_id':search.task_id,'revision':1,'content':'old budget'}]
        ids,kept=merge_inflight_plan(state,[order,search],[search.task_id],outputs,None)
        self.assertEqual(ids,[order.task_id,search.task_id]);self.assertEqual(kept,[outputs[0]])
        self.assertEqual(merge_inflight_plan(state,[order,search],[],outputs,'ambiguous'),([],[]))
