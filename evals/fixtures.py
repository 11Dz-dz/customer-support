"""随机临时订单、会话与严格限定的清理；不修改源订单。"""
import secrets
from uuid import uuid4

from conversation_persistence import get_checkpointer
from postgres_config import connect_database
from session_identity import bind_session_customer


class CaseFixture:
    def __init__(self, actor):
        self.actor = actor
        self.tag = "EVAL-" + uuid4().hex
        self.thread_id = "eval-suite-" + uuid4().hex
        self.order_ids = []
        self.threads = []

    def __enter__(self):
        try:
            with connect_database() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(48151336)")
                self.owner = conn.execute("SELECT customer_id FROM orders WHERE order_id=43659").fetchone()["customer_id"]
                self.other = conn.execute("SELECT customer_id FROM orders WHERE order_id=43660").fetchone()["customer_id"]
                while True:
                    base = 1_500_000_000 + secrets.randbelow(300_000_000)
                    ids = [base, base + 1, base + 2]
                    if not conn.execute("SELECT 1 FROM orders WHERE order_id=ANY(%s) UNION ALL "
                                        "SELECT 1 FROM order_items WHERE order_item_id=ANY(%s)", (ids, ids)).fetchone():
                        break
                for oid, customer, product, quantity, price in (
                    (base, self.owner, 776, 2, 100), (base + 1, self.other, 749, 1, 123),
                ):
                    conn.execute("""INSERT INTO orders(order_id,order_number,customer_id,status_code,purchase_at,due_at,
                                    shipped_at,subtotal,tax_amount,freight,total_due)
                                    VALUES (%s,%s,%s,5,'2026-01-01','2026-01-08','2026-01-02',%s,0,0,%s)""",
                                 (oid, self.tag + str(oid), customer, quantity * price, quantity * price))
                    conn.execute("""INSERT INTO order_items(order_item_id,order_id,product_id,quantity,unit_price,
                                    unit_price_discount,line_total) VALUES (%s,%s,%s,%s,%s,0,%s)""",
                                 (oid, oid, product, quantity, price, quantity * price))
                self.order_ids = ids[:2]
            self.slots = {"own_order": base, "foreign_order": base + 1, "missing_order": base + 2,
                          "source_own": 43659, "source_other": 43660,
                          "own_customer": self.owner, "other_customer": self.other}
            self.threads.append(self.thread_id)
            self.customer_id = None if self.actor == "guest" else (self.other if self.actor == "other" else self.owner)
            if self.customer_id is not None:
                bind_session_customer(self.thread_id, self.customer_id)
            return self
        except BaseException:
            self.cleanup()
            raise

    def refund_counts(self):
        with connect_database() as conn:
            rows = conn.execute("SELECT order_id,count(*) AS n FROM refund_requests WHERE order_id=ANY(%s) GROUP BY order_id",
                                (self.order_ids,)).fetchall()
        counts = {oid: 0 for oid in self.order_ids}
        counts.update({r["order_id"]: r["n"] for r in rows})
        return counts

    def cleanup(self):
        for tid in self.threads:
            get_checkpointer().delete_thread(tid)
            with connect_database() as conn:
                if conn.execute("SELECT to_regclass('customer_service.human_handoffs') AS name").fetchone()['name']:
                    conn.execute('DELETE FROM customer_service.human_handoffs WHERE thread_id=%s', (tid,))
                conn.execute("DELETE FROM customer_service.session_customers WHERE thread_id=%s", (tid,))
        if self.order_ids:
            with connect_database() as conn:
                rows = conn.execute("SELECT order_id,order_number FROM orders WHERE order_id=ANY(%s) FOR UPDATE",
                                    (self.order_ids,)).fetchall()
                if len(rows) != 2 or any(not r["order_number"].startswith(self.tag) for r in rows):
                    raise RuntimeError("临时订单标记不一致，停止自动清理")
                conn.execute("DELETE FROM refund_requests WHERE order_id=ANY(%s)", (self.order_ids,))
                conn.execute("DELETE FROM order_items WHERE order_id=ANY(%s)", (self.order_ids,))
                conn.execute("DELETE FROM orders WHERE order_id=ANY(%s) AND order_number LIKE %s",
                             (self.order_ids, self.tag + "%"))
        self.order_ids, self.threads = [], []

    def __exit__(self, exc_type, exc_value, traceback):
        self.cleanup()


def source_fingerprint():
    result = {}
    with connect_database() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for table, key in (("orders", "order_id"), ("order_items", "order_item_id"), ("product_catalog", "product_id")):
            # 表和主键来自固定代码，不接受模型或命令行输入。
            result[table] = conn.execute(
                f"SELECT count(*) AS count,md5(string_agg(md5(row_to_json(t)::text),'' ORDER BY {key})) AS digest FROM {table} t"
            ).fetchone()
    return result
