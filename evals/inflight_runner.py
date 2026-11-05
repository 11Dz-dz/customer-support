"""Focused, explicit live acceptance of final answers after mid-run user input."""
import argparse
from contextlib import contextmanager
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import secrets
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from evals.inflight_answers import VERSION,catalog_oracle,product_answer_checks,task_checks,check,outcome


CASES=[{'id':f'{phase}-{budget}','phase':phase,'budget':budget,
        'first':'推荐一款自行车。','second':f'预算{budget}以内。','mode':'steer'}
       for phase in ['before_planning','after_initial_search','late_after_seal'] for budget in [500,1000]]
CASES.append({'id':'followup-404','phase':'followup','first':'你好','second':'本客服系统网页返回HTTP 404，解释一下含义。','mode':'followup'})


def dump(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')


@contextmanager
def fixture(project,stream):
    from fastapi.testclient import TestClient
    from psycopg import sql
    from postgres_config import connect_database
    from webapp.store import WebStore
    from webapp.runtime import WebRuntime
    from webapp.api import create_app
    from conversation_persistence import get_checkpointer
    schema='inflight_eval_'+uuid4().hex
    with connect_database() as c:c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    threads=[]
    def connect():
        c=connect_database()
        c.execute(sql.SQL('SET search_path TO {},customer_service,public').format(sql.Identifier(schema)))
        return c
    try:
        store=WebStore(connect);store.initialize()
        password=secrets.token_urlsafe(24)
        user=store.create_user('evaluation-user',password,'临时评测用户')
        runtime=WebRuntime(store,stream=stream,graph=project.app,lease_key=500000000+uuid4().int%1000000000)
        application=create_app(store,runtime,base_url='http://testserver')
        with TestClient(application,raise_server_exceptions=False) as client:
            response=client.post('/api/auth/login',json={'username':'evaluation-user','password':password},
                                 headers={'Origin':'http://testserver'})
            assert response.status_code==200,'Fixture login failed'
            headers={'Origin':'http://testserver','X-CSRF-Token':response.json()['csrf_token']}
            response=client.post('/api/conversations',json={},headers=headers)
            assert response.status_code==201,'Fixture conversation failed'
            cid=response.json()['conversation_id'];conv=store.owned(user['user_id'],cid)
            threads.append(conv['thread_id'])
            yield client,headers,cid,conv['thread_id'],store,user
    finally:
        for thread in threads:
            get_checkpointer().delete_thread(thread)
            with connect_database() as c:
                c.execute('DELETE FROM customer_service.human_handoffs WHERE thread_id=%s',(thread,))
                c.execute('DELETE FROM customer_service.session_customers WHERE thread_id=%s',(thread,))
        if not schema.startswith('inflight_eval_') or len(schema)!=46:raise RuntimeError('Invalid fixture cleanup target')
        with connect_database() as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def run_case(spec,project,reference,directory):
    from chat_runtime import stream_turn
    from webapp.inbox import WebInbox
    from evals.observer import EvaluationObserver
    from tool_result import normalize_result
    entered,release=threading.Event(),threading.Event()
    real_seed,real_poll=project.seed_product_search,WebInbox.poll
    used=False;observer=EvaluationObserver(set())
    def stream(graph,message,thread):
        nonlocal used
        if spec['phase'] in {'before_planning','followup'} and not used:
            used=True;entered.set()
            if not release.wait(60):raise TimeoutError('Evaluation input timing barrier')
        yield from stream_turn(graph,message,thread,callbacks=[observer])
    def seed(*args,**kwargs):
        nonlocal used
        result=real_seed(*args,**kwargs)
        if spec['phase']=='after_initial_search' and not used:
            used=True;entered.set()
            if not release.wait(60):raise TimeoutError('Evaluation search timing barrier')
        return result
    def poll(inbox,*,seal=False):
        nonlocal used
        rows=real_poll(inbox,seal=seal)
        if spec['phase']=='late_after_seal' and seal and not rows and not used:
            used=True;entered.set()
            if not release.wait(60):raise TimeoutError('Evaluation delivery timing barrier')
        return rows
    result={'id':spec['id'],'spec':spec,'oracle':reference,'transport_pass':False,
            'business_pass':False,'acceptance_pass':False}
    try:
        # These patches delay checkpoints only. Real router, DS, retrieval, tools,
        # graph, answer rendering and PostgreSQL stay unchanged.
        with patch.object(project,'seed_product_search',seed),patch.object(WebInbox,'poll',poll),fixture(project,stream) as f:
            client,headers,cid,thread,store,user=f
            first=client.post(f'/api/conversations/{cid}/messages',headers=headers,
                json={'message':spec['first'],'client_request_id':str(uuid4())})
            assert first.status_code==202,'Initial request not accepted'
            rid=first.json()['run_id']
            if not entered.wait(60):
                release.set();raise AssertionError('Expected checkpoint not reached')
            second=client.post(f'/api/conversations/{cid}/messages',headers=headers,
                json={'message':spec['second'],'client_request_id':str(uuid4()),'mode':spec['mode']})
            release.set();assert second.status_code==202,'Second request not accepted'
            deadline=time.monotonic()+150
            while time.monotonic()<deadline:
                run=store.run(user['user_id'],rid)
                if run['status'] not in {'pending','running'}:break
                time.sleep(.1)
            else:raise TimeoutError('Evaluation run not settled')
            history=client.get(f'/api/conversations/{cid}/messages')
            assert history.status_code==200,'History could not be loaded'
            messages=history.json()['messages'];answers=[m['content'] for m in messages if m['role']=='assistant']
            state=project.app.get_state({'configurable':{'thread_id':thread}}).values
            tasks=state.get('tasks',[]);evidence=[]
            for m in state.get('messages',[]):
                if getattr(m,'type',None)=='tool':
                    value=normalize_result(m.content,m.name,transport_error=m.status=='error')
                    evidence.append({'tool':m.name,'call_id':m.tool_call_id,'status':value.status,'data':value.payload()})
            receipts=store.input_history(cid)
            trace=[{'name':c['name'],'args':c['args']} for m in state.get('messages',[]) for c in getattr(m,'tool_calls',[])]
            transport=[check('same_worker',rid==second.json()['run_id']),check('run_completed',run['status']=='completed'),
                check('two_user_messages',sum(m['role']=='user' for m in messages)==2),
                check('all_inputs_delivered',all(r['phase']=='answered' for r in receipts)),
                check('expected_answer_count',len(answers)==(2 if spec['phase'] in {'followup','late_after_seal'} else 1))]
            text=answers[-1] if answers else ''
            if spec['phase']=='followup':
                business=[check('correct_404_meaning','404' in text and '资源未找到' in text),
                          check('no_conflicting_404_meaning',not any(x in text for x in ['权限不足','服务器内部错误','支付成功']))]
            else:
                business=task_checks(tasks,spec['budget'])+product_answer_checks(text,reference)
                # Confirm actual latest search used the updated hard budget/object.
                searches=[c for c in trace if c['name']=='search_products']
                business.append(check('updated_budget_reached_search',bool(searches) and
                    searches[-1]['args'].get('max_price')==spec['budget'] and
                    searches[-1]['args'].get('product_category')=='bicycles'))
            result.update(answers=answers,tasks=tasks,evidence=evidence,tool_calls=trace,models=observer.models,
                transport_checks=transport,business_checks=business,transport_pass=outcome(transport)['passed'],
                business_pass=outcome(business)['passed'],failed_business_checks=outcome(business)['failed_checks'],
                public_events=store.events(rid,0))
            result['acceptance_pass']=result['transport_pass'] and result['business_pass']
    except Exception as exc:
        result.update(error_type=type(exc).__name__,error=str(exc)[:500],models=observer.models)
    finally:release.set()
    dump(directory/(spec['id']+'.json'),result)
    return result


def historical_checks(path,references):
    old=json.loads(path.read_text(encoding='utf-8'));results=[]
    for case in old.get('cases',[]):
        if case.get('phase')=='followup':continue
        answers=case.get('answers',[]);oracle=references[500]
        checks=product_answer_checks(answers[-1] if answers else '',oracle)
        results.append({'phase':case.get('phase'),'original_pass':case.get('passed'),
                        'answers':answers,'independent_reference':oracle,'business_checks':checks,**outcome(checks)})
    return {'original_file':str(path),'original_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'method':'Offline final-answer review against current independent SQL snapshot; original report unchanged',
            'source_time_limitation':'Current snapshot only, not proof of historical catalog immutability',
            'results':results}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true');parser.add_argument('--ids')
    parser.add_argument('--historical',type=Path)
    parser.add_argument('--output-root',type=Path,default=Path(__file__).parent/'reports_inflight_answers')
    args=parser.parse_args(argv)
    selected=CASES
    if args.ids:
        ids=set(args.ids.split(','));selected=[c for c in CASES if c['id'] in ids]
        if ids!={c['id'] for c in selected}:parser.error('Unknown case IDs')
    if not args.live:
        print(json.dumps({'version':VERSION,'cases':selected,'api_calls':0},ensure_ascii=False));return 0
    from postgres_config import connect_database
    from evals.fixtures import source_fingerprint
    from chat_events import configure_event_log
    from langsmith_tracing import flush_traces
    import langgraph_supervisor_1 as project
    with connect_database() as c:
        references={b:catalog_oracle(c,b) for b in [500,1000]}
    directory=args.output_root.resolve()/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8])
    directory.mkdir(parents=True,mode=0o700)
    root=Path(__file__).parents[1]
    report={'version':VERSION,'source_before':source_fingerprint(),'oracle':references,'cases':[],
            'planned_cases':len(selected),'live':True,'uses_llm_judge':False,
            'scope':'Authored functional final-answer regression, not general semantic recommendation or concurrency load acceptance',
            'code_files':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in [*root.glob('*.py'),*root.joinpath('webapp').glob('*.py'),Path(__file__),Path(__file__).with_name('inflight_answers.py')]}}
    dump(directory/'report.json',report);print('REPORT_DIR',directory,flush=True)
    try:
        if args.historical:
            dump(directory/'historical_audit.json',historical_checks(args.historical,references))
        for spec in selected:
            configure_event_log(directory/(spec['id']+'-events.jsonl'))
            result=run_case(spec,project,references.get(spec.get('budget')),directory)
            report['cases'].append(result);dump(directory/'report.json',report)
            print(json.dumps({k:result.get(k) for k in ['id','transport_pass','business_pass','failed_business_checks','error_type']},ensure_ascii=False),flush=True)
            if any(m.get('provider_status_code')==402 for m in result.get('models',[])):
                report['aborted_reason']='provider_402';break
    finally:
        report['source_after']=source_fingerprint();report['source_unchanged']=report['source_after']==report['source_before']
        cases=report['cases'];report['summary']={'planned':len(selected),'executed':len(cases),
            'transport_pass':sum(c['transport_pass'] for c in cases),'business_pass':sum(c['business_pass'] for c in cases),
            'acceptance_pass':sum(c['acceptance_pass'] for c in cases)}
        report['acceptance_pass']=len(cases)==len(selected) and all(c['acceptance_pass'] for c in cases) and report['source_unchanged']
        dump(directory/'report.json',report)
        lines=['# 追加消息最终答案验收','',f'版本：{VERSION}。流程通过与业务答案通过分开统计；不调用模型裁判。','',
               '| 场景 | 流程 | 最终业务答案 | 未通过检查 |','|---|---|---|---|']
        for c in cases:lines.append('| '+c['id']+' | '+str(c['transport_pass'])+' | '+str(c['business_pass'])+' | '+', '.join(c.get('failed_business_checks',[]))+' |')
        lines+=['','独立SQL：500以内整车'+str(references[500]['matched_count'])+'款，1000以内'+str(references[1000]['matched_count'])+'款。',
                '数据指纹未变：'+str(report['source_unchanged'])+'。整体验收：'+str(report['acceptance_pass'])+'。',
                '未修改客服业务代码；失败不自动重跑挑分。解析范围限本项目明确商品ID/目录价格输出，未知格式按覆盖不足失败。']
        (directory/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8');flush_traces()
    print(json.dumps(report['summary'],ensure_ascii=False),flush=True)
    return 0 if report['acceptance_pass'] else 1
