"""Sequential, isolated business schema; production order/refund rows stay untouched."""
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from backend.conversation.conversation_persistence import get_checkpointer
from backend.postgres_config import connect_database
from backend.conversation.session_identity import bind_session_customer


class CurrentFixture:
    def __init__(self, case):
        self.case=case;self.schema='eval_current_'+uuid4().hex
        self.thread_id='eval-current-'+uuid4().hex
        self.stack=ExitStack();self.created=False;self.customer_id=None
        self.order_ids=[1900000001,1900000002,43659]
        self.writable_order_ids=[]
        self.slots={'own_order':1900000001,'foreign_order':1900000002,'missing_order':1900000003,
                    'source_own':43659,'source_other':1900000002,'own_customer':29825,'other_customer':29672}

    def connect(self):
        from psycopg import sql
        conn=connect_database()
        conn.execute(sql.SQL('SET search_path TO {},customer_service,public').format(sql.Identifier(self.schema)))
        return conn

    def __enter__(self):
        from psycopg import sql
        try:
            with connect_database() as c:
                c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)));self.created=True
            with self.connect() as c:
                c.execute('CREATE TABLE orders (LIKE customer_service.orders INCLUDING ALL)')
                c.execute('CREATE TABLE order_items (LIKE customer_service.order_items INCLUDING ALL)')
                c.execute('INSERT INTO orders SELECT * FROM customer_service.orders WHERE order_id=43659')
                c.execute('INSERT INTO order_items SELECT * FROM customer_service.order_items WHERE order_id=43659')
                for oid,customer,pid,qty,price in [(1900000001,29825,776,2,100),(1900000002,29672,749,1,123)]:
                    c.execute('''INSERT INTO orders(order_id,order_number,customer_id,status_code,purchase_at,due_at,
                        shipped_at,subtotal,tax_amount,freight,total_due) VALUES(%s,%s,%s,5,'2026-01-01',
                        '2026-01-08','2026-01-02',%s,0,0,%s)''',(oid,'SO'+str(oid),customer,qty*price,qty*price))
                    c.execute('''INSERT INTO order_items(order_item_id,order_id,product_id,quantity,unit_price,
                        unit_price_discount,line_total) VALUES(%s,%s,%s,%s,%s,0,%s)''',(oid,oid,pid,qty,price,qty*price))
                c.execute('''CREATE TABLE refund_requests(refund_id text PRIMARY KEY,order_id integer NOT NULL REFERENCES orders,
                    reason text NOT NULL,status text NOT NULL DEFAULT 'pending',created_at timestamptz NOT NULL DEFAULT now())''')
                c.execute('CREATE UNIQUE INDEX active_refund ON refund_requests(order_id) WHERE status IN (\'pending\',\'approved\')')
                c.execute(Path(__file__).parents[1].joinpath('database/schema_refunds.sql').read_text(encoding='utf-8'))
                for slot,status in self.case.get('fixture_refunds',{}).items():
                    c.execute('INSERT INTO refund_requests(refund_id,order_id,reason,status) VALUES(%s,%s,%s,%s)',
                        ('RF-EVAL-'+slot,self.slots[slot],'评测初始化申请，聊天不得修改',status))
            # These modules hold imported connections. Patch only in this sequential evaluation process.
            for module in ['backend.orders.adventureworks_service','evals.grading']:
                self.stack.enter_context(patch(module+'.connect_database',self.connect))
            self.customer_id=None if self.case['actor']=='guest' else (29672 if self.case['actor']=='other' else 29825)
            if self.customer_id is not None:bind_session_customer(self.thread_id,self.customer_id)
            self.before=self.snapshot()
            return self
        except BaseException:
            self.__exit__(None,None,None)
            raise

    def snapshot(self):
        with self.connect() as c:
            return {table:c.execute(f'SELECT row_to_json(t) AS row FROM {table} t ORDER BY row_to_json(t)::text').fetchall()
                    for table in ['orders','order_items','refund_requests','refund_application_details',
                                  'refund_submission_keys','refund_reviews']}

    def __exit__(self,*args):
        try:
            get_checkpointer().delete_thread(self.thread_id)
            with connect_database() as c:
                c.execute('DELETE FROM customer_service.human_handoffs WHERE thread_id=%s',(self.thread_id,))
                c.execute('DELETE FROM customer_service.session_customers WHERE thread_id=%s',(self.thread_id,))
        finally:
            self.stack.close()
            if self.created:
                from psycopg import sql
                if not self.schema.startswith('eval_current_') or len(self.schema)!=45:
                    raise RuntimeError('Invalid evaluation schema; refusing cleanup')
                with connect_database() as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
                self.created=False
