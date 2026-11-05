"""Web ownership, opaque login sessions and durable public run events."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
from uuid import uuid4
from psycopg.errors import UniqueViolation

from postgres_config import connect_database


class WebError(Exception):
    def __init__(self,status,code,message):
        self.status,self.code,self.message=status,code,message
        super().__init__(message)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password):
    if not 12<=len(password)<=256:raise ValueError('密码长度需为12–256字符')
    salt=secrets.token_bytes(16)
    value=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1,dklen=32)
    return 'scrypt$'+salt.hex()+'$'+value.hex()


def verify_password(password,encoded):
    try:
        _,salt,wanted=encoded.split('$')
        actual=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1,dklen=32)
        return hmac.compare_digest(actual.hex(),wanted)
    except (ValueError,TypeError):return False


class WebStore:
    def __init__(self,connect=connect_database):
        self.connect=connect
        self.dummy_hash=hash_password(secrets.token_urlsafe(24))

    def initialize(self):
        with self.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(48151340)')
            c.execute((Path(__file__).resolve().parents[1]/'schema_web.sql').read_text())

    def create_user(self,username,password,display_name,role='user',customer_id=None):
        if role not in {'user','admin'}:raise ValueError('无效角色')
        if not username or len(username)>64 or not username.isascii() or not all(x.isalnum() or x in '-_.' for x in username):
            raise ValueError('账号需为1–64位英文、数字、点、横线或下划线')
        if not display_name.strip() or len(display_name)>80:raise ValueError('显示名称长度需为1–80字符')
        encoded=hash_password(password)
        with self.connect() as c:
            if customer_id is not None and not c.execute('SELECT 1 FROM orders WHERE customer_id=%s LIMIT 1',(customer_id,)).fetchone():
                raise ValueError('客户编号不在样例订单中')
            row=c.execute('''INSERT INTO web_users(user_id,username,display_name,password_hash,role,customer_id)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING user_id,username,role''',
                (uuid4(),username.lower(),display_name.strip(),encoded,role,customer_id)).fetchone()
        return row

    def register(self,username,password,ip):
        # Never accept role/customer identity/display name from public callers.
        # Commit admission separately: rejected duplicates also consume an attempt.
        limits=[('register:global',60),('register:ip:'+digest(ip),10)]
        with self.connect() as c:
            c.execute("DELETE FROM web_registration_limits WHERE window_start<now()-interval '1 day'")
            now=datetime.now(timezone.utc)
            for key,maximum in limits:
                c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,48151344))',(key,))
                row=c.execute('SELECT * FROM web_registration_limits WHERE key=%s',(key,)).fetchone()
                if row and row['window_start']>now-timedelta(minutes=15) and row['attempts']>=maximum:
                    raise WebError(429,'REGISTRATION_LIMIT','注册尝试较多，请15分钟后再试')
            for key,_ in limits:
                c.execute("""INSERT INTO web_registration_limits VALUES (%s,1,now()) ON CONFLICT(key) DO UPDATE
                    SET attempts=CASE WHEN web_registration_limits.window_start<now()-interval '15 minutes' THEN 1 ELSE web_registration_limits.attempts+1 END,
                    window_start=CASE WHEN web_registration_limits.window_start<now()-interval '15 minutes' THEN now() ELSE web_registration_limits.window_start END""",(key,))
        try:
            return self.create_user(username,password,username,role='user',customer_id=None)
        except UniqueViolation:
            raise WebError(409,'USERNAME_TAKEN','该账号已被使用，请换一个账号') from None

    def login(self,username,password,ip):
        key=digest(username.lower()+'|'+ip)
        with self.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,48151341))',(key,))
            limit=c.execute('SELECT * FROM web_login_limits WHERE key=%s',(key,)).fetchone()
            now=datetime.now(timezone.utc)
            if limit and limit['window_start']>now-timedelta(minutes=15) and limit['failures']>=5:
                raise WebError(429,'LOGIN_LIMIT','尝试次数较多，请15分钟后再试')
            user=c.execute('SELECT * FROM web_users WHERE username=%s',(username.lower(),)).fetchone()
            ok=verify_password(password,user['password_hash'] if user else self.dummy_hash)
            if not ok or not user or not user['active']:
                c.execute('''INSERT INTO web_login_limits VALUES (%s,1,now()) ON CONFLICT(key) DO UPDATE
                  SET failures=CASE WHEN web_login_limits.window_start<now()-interval '15 minutes' THEN 1 ELSE web_login_limits.failures+1 END,
                      window_start=CASE WHEN web_login_limits.window_start<now()-interval '15 minutes' THEN now() ELSE web_login_limits.window_start END''',(key,))
                c.commit()
                raise WebError(401,'LOGIN_FAILED','账号或密码不正确')
            token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(24)
            c.execute('DELETE FROM web_login_limits WHERE key=%s',(key,))
            c.execute('DELETE FROM web_sessions WHERE expires_at<now()')
            c.execute("INSERT INTO web_sessions VALUES (%s,%s,%s,now()+interval '12 hours')",(digest(token),user['user_id'],csrf))
        return token,csrf,user

    def authenticate(self,token):
        if not token:raise WebError(401,'LOGIN_REQUIRED','请先登录')
        with self.connect() as c:
            user=c.execute('''SELECT u.user_id,u.username,u.display_name,u.role,u.customer_id,s.csrf_token
                FROM web_sessions s JOIN web_users u ON u.user_id=s.user_id
                WHERE s.token_hash=%s AND s.expires_at>now() AND u.active''',(digest(token),)).fetchone()
        if not user:raise WebError(401,'LOGIN_REQUIRED','登录已失效，请重新登录')
        return user

    def logout(self,token):
        with self.connect() as c:c.execute('DELETE FROM web_sessions WHERE token_hash=%s',(digest(token),))

    def owned(self,user_id,conversation_id,conn=None,lock=False):
        if conn is None:
            with self.connect() as c:return self.owned(user_id,conversation_id,c,lock)
        row=conn.execute('SELECT * FROM web_conversations WHERE conversation_id=%s AND user_id=%s'+(' FOR UPDATE' if lock else ''),
                         (conversation_id,user_id)).fetchone()
        if not row:raise WebError(404,'NOT_FOUND','会话不存在')
        return row

    def create_conversation(self,user,title):
        from session_identity import bind_session_customer
        cid=uuid4();thread='web-'+uuid4().hex
        # Trusted user/customer mapping comes from the administrator-created account.
        if user['customer_id'] is not None:bind_session_customer(thread,user['customer_id'])
        with self.connect() as c:
            c.execute('INSERT INTO web_conversations(conversation_id,user_id,thread_id,title) VALUES (%s,%s,%s,%s)',
                      (cid,user['user_id'],thread,title))
        return {'conversation_id':cid,'title':title}

    def conversations(self,user_id):
        with self.connect() as c:
            return c.execute('''SELECT conversation_id,title,updated_at FROM web_conversations
                WHERE user_id=%s ORDER BY updated_at DESC LIMIT 100''',(user_id,)).fetchall()

    def reserve_run(self,user_id,cid,client_id,message,capacity,mode='steer'):
        if mode not in {'steer','followup'}:raise WebError(422,'INVALID_INPUT','无效消息处理方式')
        with self.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(48151342)')
            conv=self.owned(user_id,cid,c,True)
            receipt=c.execute('''SELECT i.*,r.status FROM web_run_inputs i JOIN web_runs r USING(run_id)
                WHERE i.conversation_id=%s AND i.client_request_id=%s''',(cid,client_id)).fetchone()
            if receipt:
                if receipt['message']!=message:raise WebError(409,'REQUEST_CONFLICT','同一请求编号不能发送不同内容')
                return receipt,False
            previous=c.execute('SELECT run_id,status,message FROM web_runs WHERE conversation_id=%s AND client_request_id=%s',(cid,client_id)).fetchone()
            if previous:
                if previous['message']!=message:raise WebError(409,'REQUEST_CONFLICT','同一请求编号不能发送不同内容')
                return previous,False
            active=c.execute("SELECT run_id,status,input_closed,cancel_requested FROM web_runs WHERE conversation_id=%s AND status IN ('pending','running')",(cid,)).fetchone()
            if active:
                if active['cancel_requested']:raise WebError(409,'STOPPING','本轮正在停止，请等待收尾后重新发送')
                count=c.execute('SELECT count(*) AS n FROM web_run_inputs WHERE run_id=%s',(active['run_id'],)).fetchone()['n']
                if count>=8:raise WebError(429,'INPUT_QUEUE_FULL','本轮已接收较多消息，请等待处理后再发送')
                queued=c.execute("SELECT 1 FROM web_run_inputs WHERE run_id=%s AND phase='pending' AND mode='followup' LIMIT 1",(active['run_id'],)).fetchone()
                delivery='followup' if active['input_closed'] or mode=='followup' or queued else 'steer'
                row=self._insert_input(c,active['run_id'],cid,client_id,message,delivery)
                return {**active,**row,'new_input':True},False
            count=c.execute("SELECT count(*) AS n FROM web_runs WHERE status IN ('pending','running')").fetchone()['n']
            if count>=capacity:raise WebError(429,'SERVER_BUSY','客服正在处理其他请求，请稍后重试')
            rid=uuid4()
            c.execute("INSERT INTO web_runs(run_id,conversation_id,client_request_id,message,status) VALUES (%s,%s,%s,%s,'pending')",(rid,cid,client_id,message))
            row=self._insert_input(c,rid,cid,client_id,message,'steer')
            c.execute('UPDATE web_conversations SET updated_at=now(),title=CASE WHEN title=%s THEN %s ELSE title END WHERE conversation_id=%s',('新对话',message[:40],cid))
        return {'run_id':rid,'status':'pending','thread_id':conv['thread_id'],**row},True

    @staticmethod
    def _input_event(c,rid,row):
        payload={'message_id':str(row['input_id']),'content':row['message'],
                 'mode':row['mode'],'phase':row['phase']}
        c.execute("INSERT INTO web_run_events(run_id,event_type,payload) VALUES (%s,'input',%s::jsonb)",
                  (rid,json.dumps(payload,ensure_ascii=False)))

    def _insert_input(self,c,rid,cid,client_id,message,mode):
        row=c.execute('''INSERT INTO web_run_inputs(input_id,run_id,conversation_id,client_request_id,message,mode)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *''',(uuid4(),rid,cid,client_id,message,mode)).fetchone()
        self._input_event(c,rid,row)
        return row

    def initial_input(self,rid):
        with self.connect() as c:
            return c.execute('SELECT * FROM web_run_inputs WHERE run_id=%s ORDER BY sequence_id LIMIT 1',(rid,)).fetchone()

    def claim_input(self,rid,input_id,batch):
        with self.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(48151342)')
            row=c.execute("UPDATE web_run_inputs SET phase='included',batch_id=%s WHERE run_id=%s AND input_id=%s AND phase='pending' RETURNING *",(batch,rid,input_id)).fetchone()
            if row:self._input_event(c,rid,row)

    def poll_inputs(self,rid,batch,*,seal=False):
        with self.connect() as c:
            # Admission and final publication share this transaction lock: input
            # is either included before publication or is a durable follow-up.
            c.execute('SELECT pg_advisory_xact_lock(48151342)')
            run=c.execute('SELECT cancel_requested FROM web_runs WHERE run_id=%s',(rid,)).fetchone()
            if run['cancel_requested']:return [],True
            rows=c.execute("SELECT * FROM web_run_inputs WHERE run_id=%s AND phase='pending' ORDER BY sequence_id",(rid,)).fetchall()
            included=[]
            for row in rows:
                if row['mode']=='followup':break
                row=c.execute("UPDATE web_run_inputs SET phase='included',batch_id=%s WHERE input_id=%s RETURNING *",(batch,row['input_id'])).fetchone()
                included.append(row);self._input_event(c,rid,row)
            if seal and not included:c.execute('UPDATE web_runs SET input_closed=true WHERE run_id=%s',(rid,))
            return included,False

    def close_input(self,rid):
        with self.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(48151342)')
            c.execute('UPDATE web_runs SET input_closed=true WHERE run_id=%s',(rid,))

    def advance_or_finish(self,rid,batch):
        with self.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(48151342)')
            answered=c.execute("UPDATE web_run_inputs SET phase='answered' WHERE run_id=%s AND batch_id=%s AND phase='included' RETURNING *",(rid,batch)).fetchall()
            for row in answered:self._input_event(c,rid,row)
            stopped=c.execute('SELECT cancel_requested FROM web_runs WHERE run_id=%s',(rid,)).fetchone()['cancel_requested']
            row=None if stopped else c.execute("SELECT * FROM web_run_inputs WHERE run_id=%s AND phase='pending' ORDER BY sequence_id LIMIT 1",(rid,)).fetchone()
            if row:
                c.execute('UPDATE web_runs SET input_closed=false WHERE run_id=%s',(rid,))
                return row
            c.execute("UPDATE web_runs SET status='completed',finished_at=now() WHERE run_id=%s",(rid,))
            c.execute("INSERT INTO web_run_events(run_id,event_type,payload) VALUES (%s,'done','{\"status\":\"completed\"}')",(rid,))
            return None

    def stop_run(self,user_id,cid):
        with self.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(48151342)');self.owned(user_id,cid,c,True)
            run=c.execute("SELECT * FROM web_runs WHERE conversation_id=%s AND status IN ('pending','running')",(cid,)).fetchone()
            if not run:return {'requested':False,'message':'当前没有正在处理的任务'}
            if run['input_closed']:raise WebError(409,'ANSWER_COMMITTED','本轮回答已开始交付，请等待本轮结束')
            c.execute('UPDATE web_runs SET cancel_requested=true,input_closed=true WHERE run_id=%s',(run['run_id'],))
            rows=c.execute("UPDATE web_run_inputs SET phase='cancelled' WHERE run_id=%s AND phase='pending' RETURNING *",(run['run_id'],)).fetchall()
            for row in rows:self._input_event(c,run['run_id'],row)
            c.execute("INSERT INTO web_run_events(run_id,event_type,payload) VALUES (%s,'progress',%s::jsonb)",(run['run_id'],json.dumps({'message':'已请求停止，等待当前模型或工具执行结束后收尾'},ensure_ascii=False)))
            return {'requested':True,'message':'已请求停止，等待当前执行结束后收尾'}

    def input_history(self,cid):
        with self.connect() as c:
            return c.execute('''SELECT i.*,r.status AS run_status FROM web_run_inputs i JOIN web_runs r USING(run_id)
                WHERE i.conversation_id=%s ORDER BY i.sequence_id''',(cid,)).fetchall()

    def run(self,user_id,rid):
        with self.connect() as c:
            row=c.execute('''SELECT r.run_id,r.status,r.message,r.conversation_id FROM web_runs r
                JOIN web_conversations v ON r.conversation_id=v.conversation_id WHERE r.run_id=%s AND v.user_id=%s''',(rid,user_id)).fetchone()
        if not row:raise WebError(404,'NOT_FOUND','请求不存在')
        return row

    def active_run(self,cid):
        with self.connect() as c:
            return c.execute("SELECT run_id,status FROM web_runs WHERE conversation_id=%s AND status IN ('pending','running')",(cid,)).fetchone()

    def events(self,rid,after):
        with self.connect() as c:
            return c.execute('SELECT event_id,event_type,payload FROM web_run_events WHERE run_id=%s AND event_id>%s ORDER BY event_id LIMIT 100',(rid,after)).fetchall()

    def event(self,rid,kind,payload):
        with self.connect() as c:
            c.execute('INSERT INTO web_run_events(run_id,event_type,payload) VALUES (%s,%s,%s::jsonb)',(rid,kind,json.dumps(payload,ensure_ascii=False)))

    def finish(self,rid,status,kind,payload):
        with self.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(48151342)')
            c.execute("UPDATE web_run_inputs SET phase='interrupted' WHERE run_id=%s AND phase IN ('pending','included')",(rid,))
            c.execute('INSERT INTO web_run_events(run_id,event_type,payload) VALUES (%s,%s,%s::jsonb)',(rid,kind,json.dumps(payload,ensure_ascii=False)))
            c.execute('UPDATE web_runs SET status=%s,finished_at=now() WHERE run_id=%s',(status,rid))

    def tickets(self,user_id):
        with self.connect() as c:
            return c.execute('''SELECT h.ticket_id,h.status,h.reason,h.created_at,h.updated_at,h.resolution_note,v.title
                FROM human_handoffs h JOIN web_conversations v ON v.thread_id=h.thread_id
                WHERE v.user_id=%s ORDER BY h.created_at DESC LIMIT 100''',(user_id,)).fetchall()
