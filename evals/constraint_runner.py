"""Live bounded field-update acceptance: real router; separate SQL/answer E2E gate."""
import argparse
import hashlib
import json
import time
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
from langchain_core.messages import HumanMessage,AIMessage
from evals.constraint_cases import CASES,END_TO_END


def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def dump(path,value):path.write_text(json.dumps(value,ensure_ascii=False,default=str,indent=2),encoding='utf-8')


def plan_case(project,spec,repetition):
    from backend.tasks.task_state import ConversationTask,TaskParameters
    from evals.observer import EvaluationObserver
    from backend.conversation.chat_runtime import turn_config
    inputs=spec['inputs'];ids=[uuid4().hex for _ in inputs]
    tasks=[];messages=[]
    kind=spec.get('kind','product_search')
    if spec['old']:
        old=ConversationTask(kind=kind,agent='sales',parameters=TaskParameters(**spec['old']),query='已有商品任务',status='completed')
        tasks=[old.model_dump()]
        messages=[HumanMessage(content='之前的商品条件已记录。'),AIMessage(content='可以继续修改当前任务的筛选条件。')]
    messages += [HumanMessage(content=text,id=uid) for text,uid in zip(inputs,ids)]
    state={'messages':messages,'tasks':tasks,'inflight_user_ids':ids,'turn_task_ids':[], 'turn_outputs':[]}
    observer=EvaluationObserver(set());config=turn_config('constraint-plan-'+uuid4().hex,callbacks=[observer])
    started=time.perf_counter()
    out=project.supervisor_node(state,config)
    by_id={t['task_id']:t for t in out.get('tasks',[])}
    selected=[by_id[k] for k in out.get('turn_task_ids',[]) if k in by_id]
    notice=out.get('plan_message');checks={}
    if spec.get('clarify'):
        checks={'clarification_present':bool(notice),'no_execution_tasks':not selected,'saved_task_unchanged':out.get('tasks')==tasks}
    else:
        selected=[t for t in selected if t['kind']==kind]
        latest=selected[-1] if selected else {}
        checks={'planned_expected_kind':bool(selected),'no_unexpected_clarification':not notice}
        params=latest.get('parameters',{})
        checks.update({key:params.get(key)==value for key,value in spec['expected'].items()})
        if tasks:
            checks['target_scope']=latest.get('task_id')!=tasks[0]['task_id'] if spec.get('new_request') else latest.get('task_id')==tasks[0]['task_id']
        checks['source_records_valid']=all(isinstance(v.get('source_index'),int) and v['source_index']<len(inputs) and v.get('source_text','') in inputs[v['source_index']] for v in latest.get('parameter_sources',{}).values())
    return {'id':spec['id'],'repeat':repetition,'inputs':inputs,'expected':spec['expected'],'tasks':out.get('tasks'),
            'active_ids':out.get('turn_task_ids'),'notice':notice,'checks':checks,'passed':all(checks.values()),
            'models':observer.models,'elapsed_ms':round((time.perf_counter()-started)*1000,2)}


def independent_reference(spec):
    from backend.postgres_config import connect_database
    # This oracle never imports business filter builders, search, ranking, or renderer.
    categories={'bicycles':(1,2,3),'components':tuple(range(4,18)), 'clothing':tuple(range(18,26)),'accessories':tuple(range(26,38))}
    where=['product_subcategory_id=ANY(%s)','list_price>0','list_price<=%s'];params=[list(categories[spec['category']]),spec['budget']]
    for key in ['color','size']:
        if key in spec:where.append(key+'=%s');params.append(spec[key])
    with connect_database() as c:return c.execute('SELECT product_id,name,list_price,product_subcategory_id,color,size FROM product_catalog WHERE '+' AND '.join(where)+' ORDER BY product_id',params).fetchall()


def end_to_end(project,spec):
    from backend.conversation.chat_runtime import stream_turn
    from evals.observer import EvaluationObserver
    from backend.conversation.conversation_persistence import get_checkpointer
    import re
    from decimal import Decimal
    thread='constraint-e2e-'+uuid4().hex;turns=[];checks={}
    try:
        for text in spec['turns']:
            observer=EvaluationObserver(set());chunks=[]
            for event in stream_turn(project.app,text,thread,callbacks=[observer]):
                if event['type']=='text':chunks.append(event['text'])
            turns.append({'input':text,'answer':''.join(chunks),'tools':observer.tools,'models':observer.models})
        out=project.app.get_state({'configurable':{'thread_id':thread}}).values
        tasks=[t for t in out.get('tasks',[]) if t['kind']=='product_search'];latest=tasks[-1] if tasks else {}
        p=latest.get('parameters',{});answer=turns[-1]['answer'];refs=independent_reference(spec);ids={int(r['product_id']):r for r in refs}
        found=list(re.finditer(r'商品\s*(\d+)\s*[:：]',answer.replace('**','')))
        checks={'task_present':bool(tasks),'category':p.get('product_category')==spec['category'],'budget':p.get('max_price')==spec['budget'],
                'count':p.get('limit')==spec['limit'],'task_finished':latest.get('status') in {'completed','empty'},'no_tool_violation':all(t['name']!='request_refund' for row in turns for t in row['tools'])}
        for field in ['color','size']:
            if field in spec:checks[field]=p.get(field)==spec[field]
        if refs:
            checks['answer_count']=len(found)==spec['limit'];checks['ids_match_independent_sql']=all(int(m[1]) in ids for m in found) and bool(found)
            for i,m in enumerate(found):
                section=answer[m.end():found[i+1].start() if i+1<len(found) else len(answer)]
                ref=ids.get(int(m[1]));prices=re.findall(r'历史目录标价\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)',section)
                checks['price_'+str(i)]=bool(ref) and len(prices)==1 and Decimal(prices[0])==Decimal(str(ref['list_price']))
        else:
            checks['truthful_empty']=not found and bool(re.search(r'(?:没有|未找到|未记录|暂无)[^。\n]{0,150}(?:符合|匹配)',answer))
            for field,label in [('color','颜色'),('size','尺寸')]:
                if field in spec:checks['empty_explains_'+field]=label in answer and spec[field] in answer
        return {'id':spec['id'],'turns':turns,'tasks':out.get('tasks'),'reference':refs,'checks':checks,'passed':all(checks.values())}
    finally:
        get_checkpointer().delete_thread(thread)
        from backend.postgres_config import connect_database
        with connect_database() as c:
            c.execute('DELETE FROM human_handoffs WHERE thread_id=%s',(thread,))
            c.execute('DELETE FROM session_customers WHERE thread_id=%s',(thread,))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');parser.add_argument('--repeat',type=int,default=2)
    parser.add_argument('--phase',choices=['plan','e2e','all'],default='all');parser.add_argument('--ids');args=parser.parse_args()
    if not args.live:print(json.dumps({'plan_scenarios':len(CASES),'plan_repetitions':args.repeat,'e2e_scenarios':len(END_TO_END),'calls':0}));return 0
    from evals.fixtures import source_fingerprint
    from backend.observability.chat_events import configure_event_log
    from backend.agent.deepseek_model import provider_settings
    import backend.agent.langgraph_supervisor_1 as project
    root=Path(__file__).parents[1];batch=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
    directory=root/'evals/reports_constraints'/batch;directory.mkdir(parents=True,exist_ok=False)
    configure_event_log(directory/'events.jsonl')
    report={'version':'1.0.0','model':provider_settings(),'source_before':source_fingerprint(),'plan':[],'end_to_end':[],
        'dataset_sha256':digest(Path(__file__).with_name('constraint_cases.py')),
        'code_files':{str(p.relative_to(root)):digest(p) for p in [*root.glob('backend/**/*.py'),*root.joinpath('webapp').glob('*.py')]},
        'limits':['Authored development regressions, not unseen holdout','Planner acceptance does not imply final-answer acceptance','No semantic suitability judge','No load acceptance','No retry to select a passing answer']}
    print('REPORT_DIR',directory,flush=True)
    selected=set(args.ids.split(',')) if args.ids else None
    try:
        if args.phase in {'plan','all'}:
            for repetition in range(1,args.repeat+1):
                for spec in CASES:
                    if selected and spec['id'] not in selected:continue
                    try:result=plan_case(project,spec,repetition)
                    except Exception as exc:result={'id':spec['id'],'repeat':repetition,'passed':False,'error_type':type(exc).__name__}
                    report['plan'].append(result);dump(directory/'report.json',report)
                    print(json.dumps({'phase':'plan','id':spec['id'],'repeat':repetition,'passed':result['passed'],'failed':[k for k,v in result.get('checks',{}).items() if not v],'notice':result.get('notice'),'error':result.get('error_type')},ensure_ascii=False),flush=True)
                    if any(m.get('provider_status_code') in [401,402,403,429] for m in result.get('models',[])):raise RuntimeError('Provider unavailable; remaining cases not run')
        if args.phase in {'e2e','all'}:
            for spec in END_TO_END:
                if selected and spec['id'] not in selected:continue
                try:result=end_to_end(project,spec)
                except Exception as exc:result={'id':spec['id'],'passed':False,'error_type':type(exc).__name__}
                report['end_to_end'].append(result);dump(directory/'report.json',report)
                print(json.dumps({'phase':'e2e','id':spec['id'],'passed':result['passed'],'failed':[k for k,v in result.get('checks',{}).items() if not v],'error':result.get('error_type')},ensure_ascii=False),flush=True)
    finally:
        report['source_after']=source_fingerprint();report['source_unchanged']=report['source_after']==report['source_before']
        report['summary']={k:{'executed':len(report[k]),'passed':sum(r['passed'] for r in report[k])} for k in ['plan','end_to_end']}
        report['passed']=report['source_unchanged'] and all(r['passed'] for k in ['plan','end_to_end'] for r in report[k]) and any(report[k] for k in ['plan','end_to_end'])
        dump(directory/'report.json',report)
    print(json.dumps({'summary':report['summary'],'passed':report['passed'],'source_unchanged':report['source_unchanged']}),flush=True)
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
