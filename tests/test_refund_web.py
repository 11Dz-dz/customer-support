"""Refund flows against isolated PostgreSQL schemas; no real funds or user orders are changed."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg import sql

from postgres_config import connect_database
from refund_service import RefundService,RefundError,ensure_refund_schema
from webapp.store import WebStore
from webapp.api import create_app


class RefundWebTests(unittest.TestCase):
    def setUp(self):
        self.schema='refund_test_'+uuid4().hex[:12]
        with connect_database() as c:
            c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        def connect():
            c=connect_database()
            c.execute(sql.SQL('SET search_path TO {},customer_service,public').format(sql.Identifier(self.schema)))
            return c
        self.connect=connect
        # Shadow all mutable business rows so even failure paths cannot touch real applications.
        with connect() as c:
            c.execute('CREATE TABLE orders (LIKE customer_service.orders INCLUDING ALL)')
            c.execute('INSERT INTO orders SELECT * FROM customer_service.orders WHERE order_id=43659')
            c.execute('CREATE TABLE order_items (LIKE customer_service.order_items INCLUDING ALL)')
            c.execute('INSERT INTO order_items SELECT * FROM customer_service.order_items WHERE order_id=43659')
            c.execute('''CREATE TABLE refund_requests (refund_id text PRIMARY KEY,order_id integer NOT NULL REFERENCES orders,
                reason text NOT NULL,status text NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','approved','rejected','cancelled')),created_at timestamptz NOT NULL DEFAULT now())''')
            c.execute("CREATE UNIQUE INDEX active_refund ON refund_requests(order_id) WHERE status IN ('pending','approved')")
        self.store=WebStore(connect);self.store.initialize();ensure_refund_schema(connect)
        self.a=self.store.create_user('alice','test-password-alice','Alice',customer_id=29825)
        self.b=self.store.create_user('bob','test-password-bobby','Bob')
        self.op=self.store.create_user('operator','test-password-admin','Operator','admin')
        self.service=RefundService(connect)
        runtime=SimpleNamespace(start=lambda:None,close=lambda:None)
        self.client=TestClient(create_app(self.store,runtime,base_url='http://testserver'),raise_server_exceptions=False)
        self.client.__enter__();self.headers=self.login('alice','test-password-alice')

    def tearDown(self):
        self.client.__exit__(None,None,None)
        with connect_database() as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def login(self,name,password):
        r=self.client.post('/api/auth/login',json={'username':name,'password':password},headers={'Origin':'http://testserver'})
        self.assertEqual(r.status_code,200,r.text)
        return {'Origin':'http://testserver','X-CSRF-Token':r.json()['csrf_token']}

    def body(self,**kw):
        return {'reason':'不再需要，申请整单处理','client_request_id':str(uuid4()),'scope':'whole_order','confirmed':True,**kw}

    def submit(self,body=None):
        return self.client.post('/api/orders/43659/refunds',json=body or self.body(),headers=self.headers)

    def test_orders_detail_and_pagination_only_expose_account_orders(self):
        r=self.client.get('/api/orders?limit=1');self.assertEqual(r.status_code,200)
        self.assertEqual([r['order_id'] for r in r.json()['orders']],[43659])
        d=self.client.get('/api/orders/43659').json();self.assertTrue(d['can_apply'])
        self.assertEqual(len(d['order']['items']),12);self.assertNotIn('customer_id',d['order'])
        self.assertEqual(self.client.get('/api/orders?before=43659').json()['orders'],[])

    def test_foreign_and_unbound_accounts_cannot_read_or_submit(self):
        self.headers=self.login('bob','test-password-bobby')
        self.assertEqual(self.client.get('/api/orders').json()['orders'],[])
        self.assertEqual(self.client.get('/api/orders/43659').status_code,404)
        self.assertEqual(self.submit().status_code,404)
        self.assertEqual(self.client.get('/api/orders/999999').json()['code'],'ORDER_NOT_ACCESSIBLE')
        with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) AS n FROM refund_requests').fetchone()['n'],0)

    def test_requires_login_and_csrf(self):
        self.assertEqual(self.client.post('/api/orders/43659/refunds',json=self.body()).status_code,403)
        self.client.post('/api/auth/logout',headers=self.headers)
        self.assertEqual(self.client.get('/api/orders').status_code,401)
        self.assertEqual(self.submit().status_code,401)

    def test_reason_confirmation_scope_and_forged_identity_are_validated(self):
        for extra in [{'reason':'  '},{'reason':'x'*2001},{'confirmed':False},{'confirmed':1},
                      {'scope':'one_item'},{'customer_id':29825},{'status':'approved'},{'amount':'0.01'}]:
            r=self.submit(self.body(**extra));self.assertEqual(r.status_code,422,(extra,r.text))
        for url in ['/api/orders/0','/api/orders/2147483648','/api/orders?limit=101']:
            self.assertEqual(self.client.get(url).status_code,422)

    def test_submit_persists_pending_and_server_amount_snapshot(self):
        r=self.submit();self.assertEqual(r.status_code,200,r.text)
        a=r.json()['application'];self.assertEqual(a['status'],'pending')
        self.assertEqual(a['order_total_snapshot'],'23153.2339');self.assertEqual(a['scope'],'whole_order')
        self.assertIn('未执行资金退款',a['funds_note'])
        self.assertFalse(self.client.get('/api/orders/43659').json()['can_apply'])
        with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) AS n FROM refund_submission_keys').fetchone()['n'],1)

    def test_order_list_and_detail_follow_refund_lifecycle_without_losing_fulfillment(self):
        def check_status(label,refund_status):
            listed=self.client.get('/api/orders').json()['orders'][0]
            detailed=self.client.get('/api/orders/43659').json()['order']
            for row in [listed,detailed]:
                self.assertEqual(row['display_status'],label)
                self.assertEqual(row['refund_status'],refund_status)
                self.assertEqual(row['fulfillment_status'],'已发货')
                self.assertEqual(row['status_code'],5)
        check_status('已发货',None)
        rid=self.submit().json()['application']['refund_id'];check_status('退款待审核','pending')
        self.service.review(self.op['user_id'],rid,'rejected','请补充信息')
        check_status('退款申请被拒绝','rejected')
        second=self.submit().json()['application']['refund_id'];check_status('退款待审核','pending')
        self.service.review(self.op['user_id'],second,'approved','资料已核对')
        check_status('退款审核通过','approved')
        # Legacy terminal rows cannot hide an active application, even if dated later.
        with self.connect() as c:
            c.execute("INSERT INTO refund_requests VALUES('LATER-CANCELLED',43659,'历史取消','cancelled',now()+interval '1 day')")
        check_status('退款审核通过','approved')

    def test_cancelled_legacy_application_and_reapplication_update_display(self):
        with self.connect() as c:c.execute("INSERT INTO refund_requests VALUES('CANCELLED',43659,'历史取消','cancelled',now())")
        self.assertEqual(self.client.get('/api/orders').json()['orders'][0]['display_status'],'退款申请已取消')
        self.assertEqual(self.client.get('/api/orders/43659').json()['order']['display_status'],'退款申请已取消')
        self.assertTrue(self.client.get('/api/orders/43659').json()['can_apply'])
        self.submit()
        self.assertEqual(self.client.get('/api/orders').json()['orders'][0]['display_status'],'退款待审核')

    def test_repeated_key_and_different_keys_reuse_active_application_without_changing_reason(self):
        body=self.body();first=self.submit(body).json()
        for repeat in [body,self.body(reason='新原因不应覆盖原原因')]:
            r=self.submit(repeat).json();self.assertTrue(r['reused'])
            self.assertEqual(r['application']['refund_id'],first['application']['refund_id'])
            self.assertEqual(r['application']['reason'],body['reason'])
        self.assertEqual(self.submit({**body,'reason':'相同编号更换内容'}).status_code,409)

    def test_concurrent_applications_create_one_record(self):
        with ThreadPoolExecutor(4) as pool:
            results=list(pool.map(lambda _:self.service.submit(self.a['user_id'],43659,'并发测试',uuid4()),range(4)))
        self.assertEqual(len({r['application']['refund_id'] for r in results}),1)
        with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) AS n FROM refund_requests').fetchone()['n'],1)

    def test_ordinary_user_cannot_review_or_list_admin_applications(self):
        rid=self.submit().json()['application']['refund_id']
        self.assertEqual(self.client.get('/api/admin/refunds').status_code,403)
        self.assertEqual(self.client.post(f'/api/admin/refunds/{rid}/review',json={'decision':'approved','note':'test'},headers=self.headers).status_code,403)
        with self.assertRaises(RefundError):self.service.review(self.a['user_id'],rid,'approved','test')

    def test_review_is_audited_without_funds_and_is_visible_to_owner(self):
        rid=self.submit().json()['application']['refund_id']
        self.headers=self.login('operator','test-password-admin')
        self.assertEqual(self.client.get('/api/admin/refunds').json()[0]['refund_id'],rid)
        url=f'/api/admin/refunds/{rid}/review'
        r=self.client.post(url,json={'decision':'approved','note':'核对后通过，仅登记审核'},headers=self.headers)
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['funds_action'],'not_executed_by_this_system')
        self.assertEqual(self.client.post(url,json={'decision':'rejected','note':'覆盖'},headers=self.headers).status_code,409)
        with self.connect() as c:
            audit=c.execute('SELECT * FROM refund_reviews').fetchall();self.assertEqual(len(audit),1)
            self.assertEqual(audit[0]['reviewer_id'],self.op['user_id'])
        self.headers=self.login('alice','test-password-alice')
        self.assertEqual(self.client.get('/api/orders/43659').json()['refunds'][0]['review_note'],'核对后通过，仅登记审核')
        self.assertTrue(self.submit().json()['reused'])

    def test_rejection_allows_new_application_but_old_key_never_replays(self):
        body=self.body();rid=self.submit(body).json()['application']['refund_id']
        self.service.review(self.op['user_id'],rid,'rejected','信息不足')
        self.assertTrue(self.client.get('/api/orders/43659').json()['can_apply'])
        old=self.submit(body).json();self.assertEqual(old['application']['status'],'rejected')
        new=self.submit().json();self.assertNotEqual(new['application']['refund_id'],rid)

    def test_concurrent_reviews_cannot_overwrite_each_other(self):
        rid=self.submit().json()['application']['refund_id']
        def review(decision):
            try:return self.service.review(self.op['user_id'],rid,decision,'review')['status']
            except RefundError as exc:return exc.code
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(review,['approved','rejected']))
        self.assertEqual(results.count('REFUND_CONFLICT'),1)
        with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) AS n FROM refund_reviews').fetchone()['n'],1)

    def test_legacy_record_preserved_and_read_status_reads_database(self):
        with self.connect() as c:c.execute("INSERT INTO refund_requests VALUES('OLD',43659,'历史原因','pending',now())")
        r=self.submit().json();self.assertTrue(r['reused']);self.assertIsNone(r['application']['scope'])
        self.assertIsNone(r['application']['order_total_snapshot'])
        from adventureworks_service import AdventureWorksService
        with patch('adventureworks_service.connect_database',self.connect):
            data=AdventureWorksService().get_refund_status('43659',customer_id=29825)
            denied=AdventureWorksService().get_refund_status('43659',customer_id=29672)
        self.assertEqual(data['refunds'][0]['refund_id'],'OLD');self.assertFalse(denied['success'])

    def test_audit_failure_rolls_back_review_and_application_failure_rolls_back_submission(self):
        with self.connect() as c:c.execute('ALTER TABLE refund_application_details ADD CONSTRAINT fail_details CHECK(false)')
        self.assertEqual(self.submit().status_code,500)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM refund_requests').fetchone()['n'],0)
            c.execute('ALTER TABLE refund_application_details DROP CONSTRAINT fail_details')
        rid=self.submit().json()['application']['refund_id']
        with self.connect() as c:c.execute('ALTER TABLE refund_reviews ADD CONSTRAINT fail_audit CHECK(false)')
        with self.assertRaises(Exception):self.service.review(self.op['user_id'],rid,'approved','test')
        self.assertEqual(self.client.get('/api/orders/43659').json()['refunds'][0]['status'],'pending')

    def test_review_validation_and_unknown_record(self):
        rid=self.submit().json()['application']['refund_id'];self.headers=self.login('operator','test-password-admin')
        for body in [{'decision':'refunded','note':'test'},{'decision':'approved','note':'  '},
                     {'decision':'approved','note':'test','reviewer_id':str(self.a['user_id'])}]:
            self.assertEqual(self.client.post(f'/api/admin/refunds/{rid}/review',json=body,headers=self.headers).status_code,422)
        self.assertEqual(self.client.post('/api/admin/refunds/no-such-refund/review',json={'decision':'approved','note':'test'},headers=self.headers).status_code,404)
        self.assertEqual(self.client.get('/api/admin/refunds?status=refunded').status_code,422)


class RefundChatTests(unittest.TestCase):
    def test_legacy_tool_reference_cannot_write_even_if_directly_invoked(self):
        import langgraph_supervisor_1 as module
        with patch.object(module.order_service,'request_refund',side_effect=AssertionError('must not execute')) as write:
            result=module.request_refund.invoke({'order_id':'43659','reason':'用户提供原因'},config={})
        self.assertEqual(result['error_code'],'CHAT_WRITE_NOT_ALLOWED');write.assert_not_called()
        self.assertNotIn('request_refund',[t.name for t in module.billing_tools])
        self.assertIn('query_refund_status',[t.name for t in module.billing_tools])

    def test_refund_focus_maps_to_read_only_status_including_mixed_queries(self):
        from task_state import ConversationTask,TaskParameters
        from task_execution import read_operations
        task=ConversationTask(kind='order_query',agent='billing',focus=['refund'],parameters=TaskParameters(order_id=43659))
        self.assertEqual(read_operations(task),[('query_refund_status',{'order_id':'43659'})])
        task.focus=['order','refund']
        self.assertEqual([n for n,_ in read_operations(task)],['query_order','query_refund_status'])

    def test_graph_reads_application_created_outside_chat_and_fresh_review_status(self):
        import langgraph_supervisor_1 as module
        from chat_runtime import invoke_turn,final_reply
        from evals.fixtures import CaseFixture
        from task_state import TaskPlan,PlannedTask,TaskParameters
        with CaseFixture('owner') as fixture:
            order=fixture.slots['own_order']
            plan=TaskPlan(tasks=[PlannedTask(next='billing',task_type='order_query',focus=['refund'],
                parameters=TaskParameters(order_id=order),query='查询退款进度')])
            rid='RF-TEST-'+uuid4().hex
            with patch.object(module,'supervisor_router',SimpleNamespace(invoke=lambda *_:plan)):
                first=final_reply(invoke_turn(module.app,'查询退款进度',fixture.thread_id)['messages']).content
                self.assertIn('没有查询到退款申请',first)
                with connect_database() as c:c.execute('INSERT INTO refund_requests(refund_id,order_id,reason) VALUES(%s,%s,%s)',(rid,order,'网页提交原因'))
                second=final_reply(invoke_turn(module.app,'再查一下退款进度',fixture.thread_id)['messages']).content
                self.assertIn('待审核',second);self.assertIn(rid,second)
                with connect_database() as c:
                    c.execute("UPDATE refund_requests SET status='approved' WHERE refund_id=%s",(rid,))
                    c.execute("INSERT INTO refund_reviews(refund_id,reviewer_id,decision,note) VALUES(%s,%s,'approved','已核对资料')",(rid,uuid4()))
                third=final_reply(invoke_turn(module.app,'现在审核结果呢',fixture.thread_id)['messages']).content
                self.assertIn('审核通过',third);self.assertIn('已核对资料',third)
                self.assertIn('未执行资金退款',third)
            self.assertEqual(fixture.refund_counts()[order],1)


if __name__=='__main__':unittest.main()
