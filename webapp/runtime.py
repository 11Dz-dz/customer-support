"""Bounded background runs. Browser disconnects never replay a business request."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging

from webapp.store import WebError
from langsmith_tracing import request_metadata_scope,flush_traces
from turn_inbox import inbox_scope,TurnStopped
from webapp.inbox import WebInbox

PROGRESS={'search_products':'正在检索商品','query_products':'正在筛选商品',
 'get_product_info':'正在查询商品详情','calculate_price':'正在核算商品小计',
 'query_order':'正在查询订单','query_my_orders':'正在查询你的订单','check_payment_status':'正在核实支付记录',
 'query_invoice':'正在核实发票记录','query_refund_status':'正在查询退款审核进度',
 'check_promotion':'正在核实优惠信息','check_system_status':'正在检查服务状态',
 'search_error_code':'正在查询错误说明'}
logger=logging.getLogger(__name__)


class WebRuntime:
    def __init__(self,store,capacity=2,stream=None,graph=None,lease_key=48151343):
        self.store,self.capacity=store,capacity
        self.stream,self.graph=stream,graph
        self.lease_key=lease_key
        self.lease=None;self.executor=None

    def start(self):
        self.lease=self.store.connect()
        acquired=self.lease.execute('SELECT pg_try_advisory_lock(%s) AS ok',(self.lease_key,)).fetchone()['ok']
        self.lease.commit()
        if not acquired:
            self.lease.close();self.lease=None
            raise RuntimeError('Web服务已在运行：当前版本请使用一个Uvicorn worker')
        # Sole service owner can safely mark runs from an earlier process interrupted.
        with self.store.connect() as c:
            stale=c.execute("SELECT run_id FROM web_runs WHERE status IN ('pending','running')").fetchall()
            for row in stale:
                c.execute("INSERT INTO web_run_events(run_id,event_type,payload) VALUES (%s,'error',%s::jsonb)",
                    (row['run_id'],'{"code":"RUN_INTERRUPTED","message":"服务重启，本轮已中断。请查看历史核实已完成事项，系统不会自动重放。"}'))
            c.execute("UPDATE web_runs SET status='interrupted',finished_at=now() WHERE status IN ('pending','running')")
            c.execute("UPDATE web_run_inputs SET phase='interrupted' WHERE phase IN ('pending','included') AND run_id IN (SELECT run_id FROM web_runs WHERE status='interrupted')")
        if self.stream is None:
            from chat_runtime import stream_turn
            from langgraph_supervisor_1 import app
            self.stream,self.graph=stream_turn,app
        self.executor=ThreadPoolExecutor(max_workers=self.capacity,thread_name_prefix='customer-web')

    def close(self):
        if self.executor:self.executor.shutdown(wait=True,cancel_futures=False)
        flush_traces()
        if self.lease:
            self.lease.execute('SELECT pg_advisory_unlock(%s)',(self.lease_key,));self.lease.commit()
            self.lease.close();self.lease=None

    def submit(self,user_id,cid,client_id,message,*,mode='steer'):
        row,new=self.store.reserve_run(user_id,cid,client_id,message,self.capacity,mode)
        if new:
            try:self.executor.submit(self._run,row['run_id'],row['thread_id'],message)
            except Exception:
                self.store.finish(row['run_id'],'failed','error',{'code':'START_FAILED','message':'本轮未能启动，请查看会话后再试'})
                raise WebError(503,'START_FAILED','本轮未能启动') from None
        return {'run_id':row['run_id'],'status':row['status'],'reused':not (new or row.get('new_input')),
                'message_id':str(row['input_id']) if row.get('input_id') else None,
                'mode':row.get('mode'),'phase':row.get('phase')}

    def _run(self,rid,thread,message):
        try:
            with self.store.connect() as c:c.execute("UPDATE web_runs SET status='running' WHERE run_id=%s",(rid,))
            self.store.event(rid,'progress',{'message':'正在理解你的问题'})
            # Consume the *whole* synchronous generator in this one worker thread.
            # ContextVar identity/deadline state never migrates between iterator threads.
            initial=self.store.initial_input(rid)
            while initial is not None:
                inbox=WebInbox(self.store,rid,initial)
                with request_metadata_scope(web_run_id=rid,entrypoint='web'),inbox_scope(inbox):
                    for part in self.stream(self.graph,initial['message'],thread):
                        if part['type']=='text':
                            inbox.ensure_sealed()
                            self.store.event(rid,'text',{'text':part['text'],'message_id':part.get('message_id') or str(inbox.batch_id)})
                        elif part['type']=='event':
                            event=part['data']
                            if event.get('event')=='tool_start':
                                self.store.event(rid,'progress',{'message':PROGRESS.get(event.get('tool'),'正在核实相关信息')})
                            elif event.get('event')=='handoff_created':
                                self.store.event(rid,'progress',{'message':'已生成待人工处理工单'})
                            elif event.get('event')=='inflight_input_consumed':
                                self.store.event(rid,'progress',{'message':'已纳入补充消息，正在更新本轮任务'})
                initial=self.store.advance_or_finish(rid,inbox.batch_id)
                if initial:self.store.event(rid,'progress',{'message':'正在处理已排队的下一条消息'})
        except TurnStopped:
            self.store.finish(rid,'interrupted','error',{'code':'RUN_STOPPED',
                'message':'本轮已停止后续处理。已完成的操作保留，待处理消息不会自动执行。'})
        except BaseException as exc:
            logger.error('web_run_failed run_id=%s error_type=%s',rid,type(exc).__name__)
            try:
                self.store.finish(rid,'failed','error',{'code':'EXECUTION_FAILED',
                    'message':'本轮未能完整处理。已保存的结果可在会话历史中查看；请核实后再决定是否重试。'})
            except Exception:logger.error('web_run_finalize_failed run_id=%s',rid)

    def history(self,user_id,cid):
        from chat_runtime import public_text
        from session_identity import validate_session_state
        conv=self.store.owned(user_id,cid)
        values=self.graph.get_state({'configurable':{'thread_id':conv['thread_id']}}).values
        receipts=self.store.input_history(cid)
        raw=list(values.get('messages',[]))
        initializing=(values.get('identity_version') is None and len(raw)==1 and
            raw[0].type=='human' and any(str(r['input_id'])==raw[0].id and
            r['run_status'] in {'pending','running'} for r in receipts))
        validate_session_state(conv['thread_id'],values,new_request=initializing)
        visible=[]
        times={}
        for m in raw:
            if m.type=='human' or m.type=='ai' and not m.tool_calls:
                visible.append({'message_id':m.id,'role':'user' if m.type=='human' else 'assistant','content':public_text(m.content)})
                if m.additional_kwargs.get('web_answer_at'):times[m.id]=m.additional_kwargs['web_answer_at']
        by_id={str(r['input_id']):r for r in receipts}
        for m in visible:
            if m['message_id'] in by_id:m.update(phase=by_id[m['message_id']]['phase'],mode=by_id[m['message_id']]['mode'])
        known={m['message_id'] for m in visible}
        visible.extend({'message_id':str(r['input_id']),'role':'user','content':r['message'],
                        'phase':r['phase'],'mode':r['mode']} for r in receipts if str(r['input_id']) not in known)
        times.update({str(r['input_id']):r['created_at'].isoformat() for r in receipts})
        # Legacy history has no delivery timestamps. Keep its existing order;
        # order new receipts and answers by actual arrival/delivery, not dequeue time.
        visible=[m for m in visible if m['message_id'] not in times]+sorted(
            [m for m in visible if m['message_id'] in times],
            key=lambda m:datetime.fromisoformat(times[m['message_id']]).timestamp())
        return {'messages':visible,'active_run':self.store.active_run(cid)}
