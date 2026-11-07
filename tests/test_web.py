"""Web contracts on temporary PostgreSQL tables; no real model calls."""
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg import sql

from backend.postgres_config import connect_database
from webapp.api import create_app
from webapp.runtime import WebRuntime
from webapp.store import WebStore,WebError,hash_password,verify_password


class WebTests(unittest.TestCase):
    def setUp(self):
        self.schema='web_test_'+uuid4().hex[:12]
        with connect_database() as c:c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        def connect():
            c=connect_database()
            c.execute(sql.SQL('SET search_path TO {},customer_service,public').format(sql.Identifier(self.schema)))
            return c
        self.store=WebStore(connect);self.store.initialize()
        self.a=self.store.create_user('alice','test-password-alice','Alice')
        self.b=self.store.create_user('bob','test-password-bobby','Bob')
        self.admin=self.store.create_user('operator','test-password-admin','Operator','admin')
        self.operator='web:'+str(self.admin['user_id'])
        self.calls=[];self.gate=threading.Event();self.gate.set()
        def stream(graph,message,thread):
            self.calls.append((message,thread))
            self.gate.wait(10)
            yield {'type':'event','data':{'event':'tool_start','tool':'query_order','reasoning_content':'NEVER_PUBLIC','args':{'private':'NEVER_PUBLIC'}}}
            yield {'type':'text','text':'已核实：39.14'}
            yield {'type':'done','answer':'NEVER_PUBLIC'}
        graph=SimpleNamespace(get_state=lambda config:SimpleNamespace(values={}))
        self.runtime=WebRuntime(self.store,capacity=2,stream=stream,graph=graph,lease_key=int(uuid4().int%1000000000)+500000000)
        self.app=create_app(self.store,self.runtime,base_url='http://testserver')
        self.client=TestClient(self.app,raise_server_exceptions=False)
        self.client.__enter__()
        self.auth=self.login('alice','test-password-alice')
        self.tickets=[]

    def tearDown(self):
        self.gate.set();self.client.__exit__(None,None,None)
        from backend.conversation.conversation_persistence import get_checkpointer
        with self.store.connect() as c:
            threads=c.execute('SELECT thread_id FROM web_conversations').fetchall()
        for row in threads:
            get_checkpointer().delete_thread(row['thread_id'])
        with connect_database() as c:
            c.execute('DELETE FROM customer_service.human_handoffs WHERE ticket_id=ANY(%s)',(self.tickets,))
            c.execute('DELETE FROM customer_service.session_customers WHERE thread_id=ANY(%s)',([r['thread_id'] for r in threads],))
            c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def login(self,name,password):
        r=self.client.post('/api/auth/login',json={'username':name,'password':password},headers={'Origin':'http://testserver'})
        self.assertEqual(r.status_code,200,r.text)
        return {'Origin':'http://testserver','X-CSRF-Token':r.json()['csrf_token']}

    def conversation(self):
        r=self.client.post('/api/conversations',json={},headers=self.auth)
        self.assertEqual(r.status_code,201,r.text)
        return r.json()['conversation_id']

    def send(self,cid,message='hello',request_id=None):
        return self.client.post(f'/api/conversations/{cid}/messages',json={'message':message,'client_request_id':request_id or str(uuid4())},headers=self.auth)

    def await_run(self,rid):
        for _ in range(100):
            run=self.store.run(self.a['user_id'],rid)
            if run['status'] not in {'running','pending'}:return run
            time.sleep(.02)
        self.fail('worker did not finish')

    def register(self,username='new-user',password='register-password',**extra):
        return self.client.post('/api/auth/register',headers={'Origin':'http://testserver'},
            json={'username':username,'password':password,'confirm_password':password,**extra})

    def test_registration_creates_unbound_user_and_requires_login(self):
        self.client.post('/api/auth/logout',headers=self.auth)
        r=self.register('New-User')
        self.assertEqual(r.status_code,201,r.text);self.assertNotIn('set-cookie',r.headers)
        self.assertEqual(self.client.get('/api/me').status_code,401)
        with self.store.connect() as c:
            row=c.execute("SELECT * FROM web_users WHERE username='new-user'").fetchone()
        self.assertEqual(row['role'],'user');self.assertIsNone(row['customer_id'])
        self.assertTrue(verify_password('register-password',row['password_hash']))
        self.assertNotEqual(row['password_hash'],'register-password')
        self.auth=self.login('NEW-USER','register-password')
        self.assertEqual(self.client.get('/api/admin/tickets').status_code,403)
        cid=self.conversation()
        from backend.conversation.session_identity import get_session_customer
        self.assertIsNone(get_session_customer(self.store.owned(row['user_id'],cid)['thread_id']))
        self.assertEqual(self.client.get('/api/conversations').json()[0]['conversation_id'],cid)

    def test_registration_rejects_privilege_and_customer_fields(self):
        for extra in [{'role':'admin'},{'customer_id':29825},{'display_name':'Operator'},{'active':True}]:
            self.assertEqual(self.register(**extra).status_code,422)
        with self.store.connect() as c:self.assertIsNone(c.execute("SELECT 1 FROM web_users WHERE username='new-user'").fetchone())

    def test_registration_rejects_bad_fields_without_echoing_password(self):
        for payload in [{'confirm_password':'different-password'}, {'username':'bad user'}, {'username':'中文账号'},
                        {'username':'ab'}, {'username':'x'*65}, {'password':'short','confirm_password':'short'}]:
            r=self.register(**payload)
            self.assertEqual(r.status_code,422,r.text)
            self.assertNotIn('register-password',r.text);self.assertNotIn('different-password',r.text)

    def test_registration_duplicate_is_case_insensitive(self):
        self.assertEqual(self.register('Mixed-Case').status_code,201)
        r=self.register('mixed-case');self.assertEqual(r.status_code,409,r.text)
        self.assertEqual(r.json()['code'],'USERNAME_TAKEN');self.assertNotIn('password',r.text)

    def test_registration_concurrent_duplicate_creates_exactly_one_account(self):
        from concurrent.futures import ThreadPoolExecutor
        def attempt(_):
            try:self.store.register('race-user','register-password','registration-race');return 201
            except WebError as e:return e.status
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
        self.assertEqual(sorted(results),[201,409])
        with self.store.connect() as c:self.assertEqual(c.execute("SELECT count(*) AS n FROM web_users WHERE username='race-user'").fetchone()['n'],1)

    def test_registration_ip_limit_counts_duplicates_and_survives_store_recreation(self):
        for _ in range(10):self.assertEqual(self.register('alice').status_code,409)
        r=self.register('limited-user');self.assertEqual(r.status_code,429,r.text)
        with self.store.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM web_users WHERE username='limited-user'").fetchone())
        with self.assertRaises(WebError) as caught:WebStore(self.store.connect).register('limited-user','register-password','testclient')
        self.assertEqual(caught.exception.code,'REGISTRATION_LIMIT')

    def test_registration_global_limit_and_expired_window(self):
        with self.store.connect() as c:c.execute("INSERT INTO web_registration_limits VALUES ('register:global',60,now())")
        with self.assertRaises(WebError) as caught:self.store.register('global-user','register-password','different-ip')
        self.assertEqual(caught.exception.code,'REGISTRATION_LIMIT')
        with self.store.connect() as c:c.execute("UPDATE web_registration_limits SET window_start=now()-interval '16 minutes'")
        self.assertEqual(self.store.register('global-user','register-password','different-ip')['role'],'user')

    def test_registration_respects_origin_and_ignores_spoofed_forwarded_ip(self):
        payload={'username':'new-user','password':'register-password','confirm_password':'register-password'}
        for headers in [{},{'Origin':'http://evil.example'}]:
            self.assertEqual(self.client.post('/api/auth/register',json=payload,headers=headers).status_code,403)
        for i in range(10):
            r=self.client.post('/api/auth/register',json={**payload,'username':'alice'},headers={'Origin':'http://testserver','X-Forwarded-For':f'10.0.0.{i}'})
            self.assertEqual(r.status_code,409)
        self.assertEqual(self.register().status_code,429)

    def test_registration_preserves_password_whitespace(self):
        password='  password-with-spaces  '
        self.assertEqual(self.register('space-user',password).status_code,201)
        self.auth=self.login('space-user',password)
        self.assertEqual(self.client.get('/api/me').json()['user']['username'],'space-user')

    def test_login_cookie_password_and_logout(self):
        self.assertTrue(verify_password('test-password-alice',hash_password('test-password-alice')))
        self.assertFalse(verify_password('wrong-password',hash_password('test-password-alice')))
        self.assertNotIn('password',self.client.get('/api/me').text)
        self.assertEqual(self.client.post('/api/auth/logout',headers=self.auth).status_code,200)
        self.assertEqual(self.client.get('/api/me').status_code,401)

    def test_csrf_origin_and_spoofed_customer_are_rejected(self):
        self.assertEqual(self.client.post('/api/conversations',json={}).status_code,403)
        self.assertEqual(self.client.post('/api/conversations',json={},headers={'Origin':'http://evil.example','X-CSRF-Token':self.auth['X-CSRF-Token']}).status_code,403)
        self.assertEqual(self.client.post('/api/conversations',json={'customer_id':29825},headers=self.auth).status_code,422)
        self.assertEqual(self.client.get('/api/me',headers={'Host':'evil.example'}).status_code,400)

    def test_login_failures_are_limited_and_do_not_echo_input(self):
        for _ in range(5):self.assertEqual(self.client.post('/api/auth/login',json={'username':'unknown','password':'not-a-password'},headers={'Origin':'http://testserver'}).status_code,401)
        self.assertEqual(self.client.post('/api/auth/login',json={'username':'unknown','password':'not-a-password'},headers={'Origin':'http://testserver'}).status_code,429)
        r=self.client.post('/api/auth/login',json={'username':'x','password':'secret'*100},headers={'Origin':'http://testserver'})
        self.assertEqual(r.status_code,422);self.assertNotIn('secret',r.text)

    def test_users_cannot_read_each_others_history_or_run(self):
        cid=self.conversation();run=self.send(cid).json();self.await_run(run['run_id'])
        self.auth=self.login('bob','test-password-bobby')
        self.assertEqual(self.client.get(f'/api/conversations/{cid}/messages').status_code,404)
        self.assertEqual(self.send(cid).status_code,404)
        self.assertEqual(self.client.get(f"/api/runs/{run['run_id']}/events").status_code,404)
        self.assertEqual(self.client.get('/api/conversations').json(),[])

    def test_duplicate_post_is_idempotent_and_conflicting_payload_is_rejected(self):
        cid=self.conversation();request=str(uuid4());first=self.send(cid,request_id=request)
        self.assertEqual(first.status_code,202,first.text);rid=first.json()['run_id'];self.await_run(rid)
        second=self.send(cid,request_id=request)
        self.assertEqual(second.json()['run_id'],rid);self.assertTrue(second.json()['reused'])
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.send(cid,'different',request).status_code,409)

    def test_same_conversation_accepts_input_and_global_capacity_is_enforced(self):
        self.gate.clear();a=self.conversation();b=self.conversation();c=self.conversation()
        self.assertEqual(self.send(a).status_code,202)
        self.assertEqual(self.send(a).status_code,202)
        self.assertEqual(self.send(b).status_code,202)
        self.assertEqual(self.send(c).status_code,429)
        self.gate.set()

    def test_sse_reconnect_uses_cursor_and_never_reexecutes(self):
        cid=self.conversation();rid=self.send(cid).json()['run_id'];self.await_run(rid)
        first=self.client.get(f'/api/runs/{rid}/events')
        self.assertEqual(first.status_code,200)
        self.assertIn('text/event-stream',first.headers['content-type'])
        self.assertIn('39.14',first.text);self.assertNotIn('NEVER_PUBLIC',first.text)
        events=self.store.events(rid,0)
        cursor=next(e['event_id'] for e in events if e['event_type']=='text')
        second=self.client.get(f'/api/runs/{rid}/events?after={cursor}')
        self.assertNotIn('39.14',second.text);self.assertIn('event: done',second.text)
        self.assertEqual(len(self.calls),1)

    def test_ordinary_user_cannot_access_admin_endpoints(self):
        self.assertEqual(self.client.get('/api/admin/tickets').status_code,403)
        self.assertEqual(self.client.post('/api/admin/tickets/x/claim',headers=self.auth).status_code,403)

    def test_ticket_owner_visibility_and_admin_claim_resolve(self):
        cid=self.conversation();conv=self.store.owned(self.a['user_id'],cid)
        ticket='WEB-TEST-'+uuid4().hex;self.tickets.append(ticket)
        with connect_database() as c:c.execute('''INSERT INTO human_handoffs(ticket_id,thread_id,customer_id,task_id,reason,subject_key,snapshot)
            VALUES (%s,%s,NULL,'t','user_requested','explicit','{}'::jsonb)''',(ticket,conv['thread_id']))
        self.assertEqual(self.client.get('/api/tickets').json()[0]['ticket_id'],ticket)
        self.auth=self.login('bob','test-password-bobby');self.assertEqual(self.client.get('/api/tickets').json(),[])
        self.auth=self.login('operator','test-password-admin')
        self.assertEqual(self.client.post(f'/api/admin/tickets/{ticket}/claim',headers=self.auth).status_code,200)
        self.assertEqual(self.client.post(f'/api/admin/tickets/{ticket}/resolve',headers=self.auth,json={'note':'已核对，无资金操作'}).status_code,200)
        self.assertEqual(self.client.get(f'/api/admin/tickets/{ticket}').json()['ticket']['status'],'resolved')

    def test_restart_marks_unfinished_run_interrupted_without_replay(self):
        cid=self.conversation()
        row,_=self.store.reserve_run(self.a['user_id'],cid,uuid4(),'do not replay',2)
        self.runtime.close();self.runtime.start()
        self.assertEqual(self.store.run(self.a['user_id'],row['run_id'])['status'],'interrupted')
        errors=[e for e in self.store.events(row['run_id'],0) if e['event_type']=='error']
        self.assertEqual(errors[-1]['payload']['code'],'RUN_INTERRUPTED')
        self.assertFalse(self.calls)

    def test_real_graph_history_excludes_tools_and_reasoning(self):
        from langchain_core.messages import AIMessage,HumanMessage,ToolMessage
        cid=self.conversation();conv=self.store.owned(self.a['user_id'],cid)
        messages=[HumanMessage(content='你好'),AIMessage(content='',tool_calls=[{'name':'x','id':'one','args':{}}],additional_kwargs={'reasoning_content':'NEVER_PUBLIC'}),ToolMessage(content='NEVER_PUBLIC',tool_call_id='one'),AIMessage(content='您好')]
        self.runtime.graph=SimpleNamespace(get_state=lambda config:SimpleNamespace(values={'identity_version':1,'identity_customer_id':None,'messages':messages}))
        r=self.client.get(f'/api/conversations/{cid}/messages')
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual([m['content'] for m in r.json()['messages']],['你好','您好'])
        self.assertNotIn('NEVER_PUBLIC',r.text)

    def test_service_lock_blocks_second_process_owner(self):
        other=WebRuntime(self.store,stream=lambda *a:[],graph=self.runtime.graph,lease_key=self.runtime.lease_key)
        with self.assertRaises(RuntimeError):other.start()

    def test_input_length_and_extra_fields(self):
        cid=self.conversation()
        self.assertEqual(self.send(cid,'x'*4001).status_code,422)
        self.assertEqual(self.client.post(f'/api/conversations/{cid}/messages',json={'message':'x','client_request_id':str(uuid4()),'customer_id':29825},headers=self.auth).status_code,422)
        self.assertEqual(self.client.post('/api/conversations',content=b'x'*20001,headers=self.auth).status_code,413)

    def test_real_graph_two_users_execute_concurrently_with_own_identity(self):
        from concurrent.futures import ThreadPoolExecutor
        import backend.agent.langgraph_supervisor_1 as project
        from backend.conversation.chat_runtime import stream_turn
        from backend.tasks.task_state import TaskPlan,PlannedTask,TaskParameters
        self.store.create_user('owner_a','owner-a-password','Owner A',customer_id=29825)
        self.store.create_user('owner_b','owner-b-password','Owner B',customer_id=29672)
        self.auth=self.login('owner_a','owner-a-password');ca=self.conversation()
        owner_a=self.store.authenticate(self.client.cookies.get('cs_session'))
        self.auth=self.login('owner_b','owner-b-password');cb=self.conversation()
        owner_b=self.store.authenticate(self.client.cookies.get('cs_session'))
        barrier=threading.Barrier(2)
        def real_stream(graph,message,thread):
            barrier.wait(timeout=10)
            yield from stream_turn(project.app,message,thread)
        class Router:
            def invoke(self,*args,**kwargs):
                # Both users ask for order 43659; only customer 29825 is authorized.
                return TaskPlan(tasks=[PlannedTask(next='billing',task_type='order_query',
                    parameters=TaskParameters(order_id=43659),focus=['order'])])
        self.runtime.stream=real_stream;self.runtime.graph=project.app
        with patch.object(project,'supervisor_router',Router()):
            a=self.runtime.submit(owner_a['user_id'],ca,uuid4(),'查询订单43659状态')
            b=self.runtime.submit(owner_b['user_id'],cb,uuid4(),'查询订单43659状态')
            for _ in range(200):
                ra=self.store.run(owner_a['user_id'],a['run_id']);rb=self.store.run(owner_b['user_id'],b['run_id'])
                if all(x['status'] not in {'pending','running'} for x in [ra,rb]):break
                time.sleep(.05)
        self.assertEqual(ra['status'],'completed');self.assertEqual(rb['status'],'completed')
        ha=self.runtime.history(owner_a['user_id'],ca);hb=self.runtime.history(owner_b['user_id'],cb)
        self.assertIn('已发货',ha['messages'][-1]['content'])
        self.assertIn('无权',hb['messages'][-1]['content'])
        self.assertNotIn('Mountain-100',hb['messages'][-1]['content'])
        with self.assertRaises(WebError):self.runtime.history(owner_b['user_id'],ca)

    def test_worker_failure_is_durable_and_duplicate_does_not_retry(self):
        def failed(*args):
            self.calls.append('failed')
            raise RuntimeError('NEVER_PUBLIC secret detail')
            yield
        self.runtime.stream=failed
        cid=self.conversation();request=str(uuid4());first=self.send(cid,request_id=request).json()
        self.assertEqual(self.await_run(first['run_id'])['status'],'failed')
        response=self.client.get(f"/api/runs/{first['run_id']}/events")
        self.assertIn('event: error',response.text);self.assertNotIn('NEVER_PUBLIC',response.text)
        self.assertEqual(self.send(cid,request_id=request).json()['run_id'],first['run_id'])
        self.assertEqual(len(self.calls),1)


if __name__=='__main__':unittest.main(verbosity=2)
