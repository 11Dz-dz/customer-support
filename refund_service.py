"""Explicit web application/review transactions. Chat has no write capability."""
from pathlib import Path
from uuid import uuid4

from adventureworks_service import json_ready, ORDER_STATUSES
from postgres_config import connect_database

FUNDS_NOTE = '本系统未接入支付渠道，未执行资金退款；审核通过不代表退款到账。'
ENTRY_GUIDANCE = ('退款申请请在网页左侧「我的订单」中打开对应订单，点击「申请退款」，'
                  '填写原因并确认提交。聊天不会代你提交申请。提交后可在订单详情查看审核进度，'
                  '也可以告诉我订单号查询。' + FUNDS_NOTE)
STATUS_LABELS = {'pending':'待审核', 'approved':'审核通过', 'rejected':'审核拒绝', 'cancelled':'已取消'}
ORDER_REFUND_LABELS = {'pending':'退款待审核', 'approved':'退款审核通过',
                       'rejected':'退款申请被拒绝', 'cancelled':'退款申请已取消'}


def order_status_fields(status_code,refund_status=None):
    """Project the current service status without overwriting historical fulfillment facts."""
    fulfillment=ORDER_STATUSES.get(status_code,'未知')
    return {'status':fulfillment,'fulfillment_status':fulfillment,'refund_status':refund_status,
            'refund_status_label':ORDER_REFUND_LABELS.get(refund_status),
            'display_status':ORDER_REFUND_LABELS.get(refund_status,fulfillment)}


class RefundError(Exception):
    def __init__(self,status,code,message):
        self.status,self.code,self.message=status,code,message
        super().__init__(message)


def ensure_refund_schema(connect=connect_database):
    with connect() as c:
        c.execute('SELECT pg_advisory_xact_lock(48151344)')
        c.execute(Path(__file__).with_name('schema_refunds.sql').read_text(encoding='utf-8'))


def application_rows(c, order_id):
    return c.execute('''SELECT r.refund_id,r.order_id,r.reason,r.status,r.created_at,
        d.scope,d.order_total_snapshot,v.decision,v.note AS review_note,v.reviewed_at
        FROM refund_requests r LEFT JOIN refund_application_details d USING(refund_id)
        LEFT JOIN refund_reviews v USING(refund_id)
        WHERE r.order_id=%s ORDER BY r.created_at DESC,r.refund_id DESC''',(order_id,)).fetchall()


def present(row):
    return json_ready({**row,'status_label':STATUS_LABELS.get(row['status'],'状态未知'),
        'scope_label':'整单' if row.get('scope')=='whole_order' else '历史申请（范围未记录）',
        'funds_action':'not_executed_by_this_system','funds_note':FUNDS_NOTE})


class RefundService:
    def __init__(self,connect=connect_database):self.connect=connect

    def _account(self,c,user_id,admin=False):
        u=c.execute('SELECT user_id,role,customer_id FROM web_users WHERE user_id=%s AND active',(user_id,)).fetchone()
        if not u:raise RefundError(401,'LOGIN_REQUIRED','请重新登录')
        if admin and u['role']!='admin':raise RefundError(403,'ADMIN_REQUIRED','仅管理员可审核')
        return u

    def _order(self,c,u,order_id,lock=False):
        row=c.execute('SELECT * FROM orders WHERE order_id=%s AND customer_id=%s'+(' FOR UPDATE' if lock else ''),
                      (order_id,u['customer_id'])).fetchone()
        if not row:raise RefundError(404,'ORDER_NOT_ACCESSIBLE','订单不存在或无权查看')
        return row

    def orders(self,user_id,before=None,limit=20):
        with self.connect() as c:
            u=self._account(c,user_id)
            rows=c.execute('''SELECT o.order_id,o.order_number,o.purchase_at,o.status_code,o.total_due,
                    latest.status AS refund_status
                FROM orders o LEFT JOIN LATERAL (
                    SELECT status FROM refund_requests WHERE order_id=o.order_id
                    ORDER BY (status IN ('pending','approved')) DESC,created_at DESC,refund_id DESC LIMIT 1
                ) latest ON true
                WHERE o.customer_id=%s AND (%s::integer IS NULL OR o.order_id<%s)
                ORDER BY o.order_id DESC LIMIT %s''',(u['customer_id'],before,before,limit+1)).fetchall()
        return json_ready({'orders':[{**r,**order_status_fields(r['status_code'],r['refund_status'])} for r in rows[:limit]],
            'next_before':rows[limit-1]['order_id'] if len(rows)>limit else None})

    def detail(self,user_id,order_id):
        with self.connect() as c:
            u=self._account(c,user_id);order=self._order(c,u,order_id)
            items=c.execute('''SELECT i.product_id,p.name AS product_name,i.quantity,i.unit_price,i.line_total
                FROM order_items i JOIN products p USING(product_id)
                WHERE i.order_id=%s ORDER BY i.order_item_id''',(order_id,)).fetchall()
            refunds=[present(r) for r in application_rows(c,order_id)]
        # Never expose raw customer identities or payment/eligibility assumptions.
        order.pop('customer_id',None)
        current=next((r for r in refunds if r['status'] in {'pending','approved'}),refunds[0] if refunds else None)
        return json_ready({'order':{**order,**order_status_fields(order['status_code'],current['status'] if current else None),
            'currency':None,'items':items},'refunds':refunds,
            'can_apply':not any(r['status'] in {'pending','approved'} for r in refunds),
            'funds_note':FUNDS_NOTE})

    def submit(self,user_id,order_id,reason,client_request_id):
        reason=reason.strip()
        if not 1<=len(reason)<=2000:raise RefundError(422,'INVALID_REFUND_REASON','请填写1–2000字符的退款原因')
        with self.connect() as c:
            u=self._account(c,user_id)
            # Serializes reuse even when the same key is maliciously sent for different orders.
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,48151345))',
                      (str(user_id)+':'+str(client_request_id),))
            order=self._order(c,u,order_id,True)
            key=c.execute('SELECT * FROM refund_submission_keys WHERE user_id=%s AND client_request_id=%s',
                          (user_id,client_request_id)).fetchone()
            if key and (key['order_id']!=order_id or key['reason']!=reason):
                raise RefundError(409,'REQUEST_CONFLICT','此提交已用于其他内容，请刷新订单后重试')
            rows=application_rows(c,order_id)
            existing=next((r for r in rows if r['refund_id']==key['refund_id']),None) if key else None
            active=next((r for r in rows if r['status'] in {'pending','approved'}),None)
            chosen=existing or active
            if not chosen:
                rid='RF-'+uuid4().hex[:12].upper()
                c.execute('INSERT INTO refund_requests(refund_id,order_id,reason) VALUES(%s,%s,%s)',(rid,order_id,reason))
                c.execute('''INSERT INTO refund_application_details(refund_id,submitted_by,scope,order_total_snapshot)
                    VALUES(%s,%s,'whole_order',%s)''',(rid,user_id,order['total_due']))
                chosen=application_rows(c,order_id)[0]
            if not key:
                c.execute('''INSERT INTO refund_submission_keys(user_id,client_request_id,order_id,reason,refund_id)
                    VALUES(%s,%s,%s,%s,%s)''',(user_id,client_request_id,order_id,reason,chosen['refund_id']))
            return {'application':present(chosen),'reused':bool(existing or active),
                    'message':'已有申请，本次没有重复创建，也没有修改原原因。' if existing or active else '申请已提交，等待管理员审核。'}

    def admin_list(self,user_id,status='pending'):
        if status not in STATUS_LABELS:raise RefundError(422,'INVALID_STATUS','无效状态')
        with self.connect() as c:
            self._account(c,user_id,True)
            rows=c.execute('''SELECT r.refund_id,r.order_id,r.reason,r.status,r.created_at,
                d.scope,d.order_total_snapshot,v.decision,v.note AS review_note,v.reviewed_at
                FROM refund_requests r LEFT JOIN refund_application_details d USING(refund_id)
                LEFT JOIN refund_reviews v USING(refund_id)
                WHERE r.status=%s ORDER BY r.created_at,r.refund_id LIMIT 100''',(status,)).fetchall()
        return [present(r) for r in rows]

    def review(self,user_id,refund_id,decision,note):
        note=note.strip()
        if decision not in {'approved','rejected'} or not 1<=len(note)<=2000:
            raise RefundError(422,'INVALID_REVIEW','请填写审核结论及1–2000字符说明')
        with self.connect() as c:
            self._account(c,user_id,True)
            row=c.execute('SELECT * FROM refund_requests WHERE refund_id=%s FOR UPDATE',(refund_id,)).fetchone()
            if not row:raise RefundError(404,'NOT_FOUND','申请不存在')
            if row['status']!='pending':raise RefundError(409,'REFUND_CONFLICT','该申请已经处理，请刷新查看最新状态')
            c.execute('UPDATE refund_requests SET status=%s WHERE refund_id=%s',(decision,refund_id))
            c.execute('INSERT INTO refund_reviews(refund_id,reviewer_id,decision,note) VALUES(%s,%s,%s,%s)',
                      (refund_id,user_id,decision,note))
            return present(next(r for r in application_rows(c,row['order_id']) if r['refund_id']==refund_id))
