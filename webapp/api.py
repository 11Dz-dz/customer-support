"""Same-origin FastAPI API; one runtime process, bounded concurrent requests."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID
from typing import Literal

from fastapi import FastAPI,Depends,Request,Response,Query,Path as PathParam
from fastapi.responses import JSONResponse,StreamingResponse,FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel,ConfigDict,Field,StrictBool,model_validator

from postgres_config import load_project_env
from webapp.store import WebStore,WebError
from webapp.runtime import WebRuntime
from refund_service import RefundService,RefundError,ensure_refund_schema


class Input(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)


class Login(BaseModel):
    model_config=ConfigDict(extra='forbid')
    username:str=Field(min_length=1,max_length=64)
    password:str=Field(min_length=1,max_length=256)


class Registration(BaseModel):
    model_config=ConfigDict(extra='forbid')
    username:str=Field(min_length=3,max_length=64,pattern=r'^[A-Za-z0-9_.-]+$')
    password:str=Field(min_length=12,max_length=256)
    confirm_password:str=Field(min_length=12,max_length=256)

    @model_validator(mode='after')
    def passwords_match(self):
        if self.password!=self.confirm_password:
            raise ValueError('password confirmation mismatch')
        return self


class Conversation(Input):
    title:str=Field(default='新对话',min_length=1,max_length=80)


class Message(Input):
    message:str=Field(min_length=1,max_length=4000)
    client_request_id:UUID
    mode:Literal['steer','followup']='steer'


class Resolution(Input):
    note:str=Field(min_length=1,max_length=2000)


class RefundApplication(Input):
    reason:str=Field(min_length=1,max_length=2000)
    client_request_id:UUID
    scope:Literal['whole_order']
    confirmed:StrictBool


class RefundReview(Resolution):
    decision:Literal['approved','rejected']


def create_app(store=None,runtime=None,base_url=None,static_dir=None):
    load_project_env()
    origin=(base_url or os.getenv('WEB_BASE_URL','http://127.0.0.1:8000')).rstrip('/')
    parsed=urlsplit(origin)
    if parsed.scheme not in {'http','https'} or not parsed.netloc or parsed.path:
        raise ValueError('WEB_BASE_URL必须是包含协议与端口的站点地址，不含路径')
    capacity=int(os.getenv('WEB_MAX_CONCURRENT_RUNS','2'))
    if not 1<=capacity<=8:raise ValueError('WEB_MAX_CONCURRENT_RUNS需为1–8')
    store=store or WebStore();runtime=runtime or WebRuntime(store,capacity)
    refunds=RefundService(store.connect)

    @asynccontextmanager
    async def lifespan(app):
        from human_handoff import ensure_handoff_schema
        from chat_events import configure_event_log
        await asyncio.to_thread(store.initialize)
        await asyncio.to_thread(ensure_refund_schema,store.connect)
        await asyncio.to_thread(ensure_handoff_schema)
        configure_event_log(Path(__file__).resolve().parents[1]/'logs/web-events.jsonl')
        await asyncio.to_thread(runtime.start)
        try:yield
        finally:
            await asyncio.to_thread(runtime.close)
            # The graph holds the shared checkpointer. Its process-level atexit
            # handler owns cleanup; closing it here breaks a second app lifespan.

    app=FastAPI(title='Customer Support',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store=store;app.state.runtime=runtime

    @app.exception_handler(RefundError)
    @app.exception_handler(WebError)
    async def web_error(request,exc):
        return JSONResponse({'code':exc.code,'message':exc.message},status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request,exc):
        return JSONResponse({'code':'INVALID_INPUT','message':'输入格式或长度不符合要求'},status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(request,exc):
        logging.getLogger(__name__).error('web_api_failed path=%s error_type=%s',request.url.path,type(exc).__name__)
        return JSONResponse({'code':'INTERNAL_ERROR','message':'服务暂时不可用，请稍后再试'},status_code=500)

    @app.middleware('http')
    async def boundary(request,call_next):
        if request.headers.get('host')!=parsed.netloc:
            return JSONResponse({'code':'INVALID_HOST','message':'访问地址与服务配置不一致'},status_code=400)
        if request.method not in {'GET','HEAD','OPTIONS'}:
            if request.headers.get('origin')!=origin:
                return JSONResponse({'code':'INVALID_ORIGIN','message':'请求来源不被允许'},status_code=403)
            try:
                if int(request.headers.get('content-length','0'))>20000:
                    return JSONResponse({'code':'BODY_TOO_LARGE','message':'输入内容过长'},status_code=413)
            except ValueError:return JSONResponse({'message':'请求格式错误'},status_code=400)
            # Also bound chunked bodies; never rely on Content-Length alone.
            size=0;chunks=[]
            async for chunk in request.stream():
                size+=len(chunk)
                if size>20000:return JSONResponse({'code':'BODY_TOO_LARGE','message':'输入内容过长'},status_code=413)
                chunks.append(chunk)
            request._body=b''.join(chunks)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control']='no-store'
        return response

    def user(request:Request):
        return store.authenticate(request.cookies.get('cs_session'))

    def mutation(request:Request,u=Depends(user)):
        if not hmac.compare_digest(request.headers.get('x-csrf-token',''),u['csrf_token']):
            raise WebError(403,'CSRF_FAILED','请求校验失败，请刷新页面')
        return u

    def admin(u=Depends(user)):
        if u['role']!='admin':raise WebError(403,'ADMIN_REQUIRED','仅管理员可访问')
        return u

    def admin_mutation(u=Depends(mutation)):
        if u['role']!='admin':raise WebError(403,'ADMIN_REQUIRED','仅管理员可操作')
        return u

    def public_user(u):return {k:u[k] for k in ['username','display_name','role']}

    @app.get('/api/health')
    def health():return {'status':'ok'}

    @app.post('/api/auth/register',status_code=201)
    def register(body:Registration,request:Request):
        store.register(body.username,body.password,request.client.host)
        return {'message':'注册成功，请登录'}

    @app.post('/api/auth/login')
    def login(body:Login,request:Request,response:Response):
        token,csrf,u=store.login(body.username.strip(),body.password,request.client.host)
        response.set_cookie('cs_session',token,max_age=43200,httponly=True,secure=parsed.scheme=='https',samesite='lax',path='/')
        return {'user':public_user(u),'csrf_token':csrf}

    @app.get('/api/me')
    def me(u=Depends(user)):return {'user':public_user(u),'csrf_token':u['csrf_token']}

    @app.post('/api/auth/logout')
    def logout(request:Request,response:Response,u=Depends(mutation)):
        store.logout(request.cookies['cs_session']);response.delete_cookie('cs_session',path='/')
        return {'ok':True}

    @app.get('/api/conversations')
    def conversations(u=Depends(user)):return store.conversations(u['user_id'])

    @app.post('/api/conversations',status_code=201)
    def create_conversation(body:Conversation,u=Depends(mutation)):
        return store.create_conversation(u,body.title)

    @app.get('/api/conversations/{cid}/messages')
    def history(cid:UUID,u=Depends(user)):
        return runtime.history(u['user_id'],cid)

    @app.post('/api/conversations/{cid}/messages',status_code=202)
    def send(cid:UUID,body:Message,u=Depends(mutation)):
        return runtime.submit(u['user_id'],cid,body.client_request_id,body.message,mode=body.mode)

    @app.post('/api/conversations/{cid}/stop')
    def stop(cid:UUID,u=Depends(mutation)):
        return store.stop_run(u['user_id'],cid)

    @app.get('/api/runs/{rid}/events')
    async def events(rid:UUID,request:Request,after:int=0,u=Depends(user)):
        if after<0:raise WebError(422,'INVALID_CURSOR','无效游标')
        await asyncio.to_thread(store.run,u['user_id'],rid)
        async def generate():
            cursor=after;heartbeat=0
            while not await request.is_disconnected():
                # Re-check session and ownership during a long-lived stream.
                try:
                    await asyncio.to_thread(store.authenticate,request.cookies.get('cs_session'))
                    run=await asyncio.to_thread(store.run,u['user_id'],rid)
                except WebError:
                    yield 'event: error\ndata: {"message":"登录已失效，请重新登录"}\n\n';return
                rows=await asyncio.to_thread(store.events,rid,cursor)
                for e in rows:
                    cursor=e['event_id']
                    yield f"id: {cursor}\nevent: {e['event_type']}\ndata: {json.dumps(e['payload'],ensure_ascii=False)}\n\n"
                if rows and rows[-1]['event_type'] in {'done','error'}:return
                if not rows and run['status'] not in {'pending','running'}:
                    return
                heartbeat+=1
                if heartbeat%20==0:yield ': keepalive\n\n'
                await asyncio.sleep(.5)
        return StreamingResponse(generate(),media_type='text/event-stream',
            headers={'X-Accel-Buffering':'no','Cache-Control':'no-store'})

    @app.get('/api/tickets')
    def tickets(u=Depends(user)):return store.tickets(u['user_id'])

    @app.get('/api/orders')
    def orders(before:int|None=Query(default=None,ge=1,le=2147483647),limit:int=Query(default=20,ge=1,le=100),u=Depends(user)):
        return refunds.orders(u['user_id'],before,limit)

    @app.get('/api/orders/{order_id}')
    def order_detail(order_id:int=PathParam(ge=1,le=2147483647),u=Depends(user)):
        return refunds.detail(u['user_id'],order_id)

    @app.post('/api/orders/{order_id}/refunds')
    def apply_refund(body:RefundApplication,order_id:int=PathParam(ge=1,le=2147483647),u=Depends(mutation)):
        if not body.confirmed:raise WebError(422,'CONFIRMATION_REQUIRED','请确认整单申请后再提交')
        return refunds.submit(u['user_id'],order_id,body.reason,body.client_request_id)

    @app.get('/api/admin/refunds')
    def refund_list(status:str='pending',u=Depends(admin)):
        return refunds.admin_list(u['user_id'],status)

    @app.post('/api/admin/refunds/{refund_id}/review')
    def refund_review(refund_id:str,body:RefundReview,u=Depends(admin_mutation)):
        return refunds.review(u['user_id'],refund_id,body.decision,body.note)

    @app.get('/api/admin/tickets')
    def admin_list(status:str='pending',u=Depends(admin)):
        from human_handoff import admin_list
        try:return admin_list(status)
        except ValueError as exc:raise WebError(422,'INVALID_STATUS',str(exc)) from None

    @app.get('/api/admin/tickets/{ticket_id}')
    def admin_show(ticket_id:str,u=Depends(admin)):
        from human_handoff import admin_show
        try:return admin_show(ticket_id)
        except ValueError:raise WebError(404,'NOT_FOUND','工单不存在') from None

    def transition(ticket,action,u,note=None):
        from human_handoff import admin_transition
        try:return admin_transition(ticket,action,'web:'+str(u['user_id']),note)
        except ValueError as exc:raise WebError(409,'TICKET_CONFLICT',str(exc)) from None

    @app.post('/api/admin/tickets/{ticket_id}/claim')
    def claim(ticket_id:str,u=Depends(admin_mutation)):return transition(ticket_id,'claim',u)

    @app.post('/api/admin/tickets/{ticket_id}/resolve')
    def resolve(ticket_id:str,body:Resolution,u=Depends(admin_mutation)):
        return transition(ticket_id,'resolve',u,body.note)

    dist=Path(static_dir or Path(__file__).resolve().parents[1]/'frontend/dist')
    if dist.is_dir():
        app.mount('/assets',StaticFiles(directory=dist/'assets'),name='assets')
        @app.get('/')
        def index():return FileResponse(dist/'index.html')
    return app


app=create_app()
