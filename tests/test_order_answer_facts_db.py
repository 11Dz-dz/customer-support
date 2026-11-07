"""Answer scorer integration against an isolated, real PostgreSQL reference table."""
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4
import unittest

from psycopg import sql
from backend.postgres_config import connect_database
from evals.current_contracts import score_current
from evals.judge_reference import load_reference


class OrderAnswerFactsDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.schema='fact_reference_'+uuid4().hex
        with connect_database() as c:
            c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        try:
            fixture,_=load_reference()
            with self.connect() as c:
                c.execute('''CREATE TABLE orders(order_id integer PRIMARY KEY,customer_id integer,
                    subtotal numeric(19,4),tax_amount numeric(19,4),freight numeric(19,4),total_due numeric(19,4))''')
                for r in fixture['orders']:
                    c.execute('INSERT INTO orders VALUES(%s,%s,%s,%s,%s,%s)',
                        tuple(r.get(k) for k in ['order_id','customer_id','subtotal','tax_amount','freight','total_due']))
            self.fixture=SimpleNamespace(customer_id=70001,slots={'own_order':12345},connect=self.connect)
        except BaseException:
            self.tearDown();raise

    @contextmanager
    def connect(self):
        with connect_database() as c:
            c.execute(sql.SQL('SET search_path TO {},public').format(sql.Identifier(self.schema)))
            yield c

    def tearDown(self):
        if not self.schema.startswith('fact_reference_') or len(self.schema)!=47:
            raise RuntimeError('Refusing to drop invalid fixture schema')
        with connect_database() as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def grade(self,text,ids,required=True):
        with self.connect() as c:
            rows=c.execute('SELECT * FROM orders WHERE order_id=ANY(%s)',(ids,)).fetchall()
        # No tool this turn: the scorer must read independent SQL rather than trusting history amounts.
        history=[{'name':'query_order','success':True,'result':{**r,'total_due':'9999'}} for r in rows]
        trace={'answer':text,'tools':[],'task_execution':[{'parameters':{'order_id':oid}} for oid in ids]}
        spec={'checks':[{'kind':'order','slot':'own_order','fact':'total'}] if required else [],'contract':{}}
        return score_current(spec,trace,self.fixture,history)

    def test_cached_answer_is_compared_with_sql_not_history_amount(self):
        checks=self.grade('订单12345，应付总额200。',[12345])
        self.assertTrue(all(c['passed'] for c in checks))

    def test_unrelated_correct_number_cannot_satisfy_sql_total(self):
        checks=self.grade('订单12345，应付总额999。商品编号是200。',[12345])
        self.assertTrue(any(c['critical'] and not c['passed'] for c in checks))

    def test_multiple_orders_are_bound_separately(self):
        good=self.grade('订单12345，应付总额200。订单12346，应付总额345.67。',[12345,12346],False)
        bad=self.grade('订单12345，应付总额345.67。订单12346，应付总额200。',[12345,12346],False)
        self.assertTrue(all(c['passed'] for c in good));self.assertFalse(all(c['passed'] for c in bad))

    def test_other_customers_order_cannot_be_reference(self):
        checks=self.grade('订单54321，应付总额900。',[54321],False)
        self.assertTrue(any(c['critical'] and not c['passed'] for c in checks))


if __name__=='__main__':unittest.main()
