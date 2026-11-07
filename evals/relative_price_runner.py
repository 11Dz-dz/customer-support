"""Replay a frozen product recommendation into new test threads; never edit its source thread."""
import argparse
from datetime import datetime,timezone
from decimal import Decimal
import hashlib,json
from pathlib import Path
from uuid import uuid4


def reference_question(text,active_ids):
    return bool(text) and not active_ids and (any(word in text for word in ['哪','确认','明确'])
        or (any(word in text for word in ['还是','或者']) and text.rstrip().endswith(('？','?'))))


def review_recorded(path):
    report=json.loads(path.read_text(encoding='utf-8'))
    assert len(report['results'])==6 and report['source_unchanged']
    checks={row['id']:all(row['checks'].values()) for row in report['results']}
    last=report['results'][-1];assert last['id']=='R06'
    turn=last['turns'][0]
    checks['R06']=last['checks']['no_tool_execution'] and last['checks']['old_task_preserved'] and reference_question(turn['answer'],turn['active_ids'])
    review={'source_report':str(path),'source_report_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'original_passed':report['passed'],'checks':checks,'passed':all(checks.values()),'new_model_calls':0,
        'reason':'R06 choice question was valid; original scorer only recognized 哪/确认/明确. No business or prompt change.',
        'evidence_answer':turn['answer'],'original_report_preserved':True}
    target=path.with_name('rules_review.json');assert not target.exists(),'Do not overwrite review evidence'
    target.write_text(json.dumps(review,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(review,ensure_ascii=False));return 0 if review['passed'] else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true')
    parser.add_argument('--snapshot',type=Path);parser.add_argument('--review',type=Path);args=parser.parse_args()
    if args.review:
        if args.live:parser.error('Choose review or live')
        return review_recorded(args.review.resolve())
    if not args.live:print('No model calls; use --live --snapshot for comparative-price regressions.');return 0
    if not args.snapshot:parser.error('--snapshot is required')
    from langchain_core.messages import messages_from_dict
    import backend.agent.langgraph_supervisor_1 as project
    from backend.conversation.chat_runtime import stream_turn
    from backend.observability.chat_events import configure_event_log
    from evals.fixtures import CaseFixture,source_fingerprint
    from evals.observer import EvaluationObserver
    from backend.postgres_config import connect_database
    from backend.conversation.session_identity import identity_guard_node
    root=Path(__file__).resolve().parents[1];batch=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
    directory=root/'evals/reports_relative_price'/batch;directory.mkdir(parents=True);directory.chmod(0o700)
    configure_event_log(directory/'events.jsonl');snapshot=json.loads(args.snapshot.read_text(encoding='utf-8'))
    report={'batch':batch,'source_before':source_fingerprint(),'snapshot_sha256':hashlib.sha256(args.snapshot.read_bytes()).hexdigest(),
        'results':[],'code_files':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [*root.glob('backend/**/*.py'),*root.glob('prompts/*.md'),*root.glob('config/*.json'),*root.glob('skills/*/SKILL.md')]},
        'limits':['Only new fixture threads are invoked','Functional and independent SQL checks, not a semantic suitability judge',
                  'No rerun to select a passing answer','Controlled empty recommendations are permitted when evidence is insufficient']}
    def save():(directory/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,default=str),encoding='utf-8')
    def turn(text,fixture):
        observer=EvaluationObserver(set());chunks=[]
        for event in stream_turn(project.app,text,fixture.thread_id,callbacks=[observer]):
            if event['type']=='text':chunks.append(event['text'])
        state=project.app.get_state({'configurable':{'thread_id':fixture.thread_id}}).values
        if any(m.get('provider_status_code') in (401,402,403,429) for m in observer.models):raise RuntimeError('Provider unavailable')
        return {'input':text,'answer':''.join(chunks),'tasks':state.get('tasks',[]),'active_ids':state.get('turn_task_ids',[]),
            'tools':observer.tools,'models':observer.models}
    def product_task(row):
        return next(t for t in row['tasks'] if t['kind']=='product_search' and (not row.get('active_ids') or t['task_id'] in row['active_ids']))
    def verify(old,row,relation,budget=None):
        task=product_task(row);pid=old['displayed_product_ids'][0]
        with connect_database() as conn:
            anchor=conn.execute('SELECT product_id,list_price FROM product_catalog WHERE product_id=%s',(pid,)).fetchone()
            ids=task['displayed_product_ids'];selected=conn.execute('SELECT product_id,list_price,product_subcategory_id FROM product_catalog WHERE product_id=ANY(%s)',(ids,)).fetchall()
        price=Decimal(str(anchor['list_price']));expected=price+{'lt':Decimal('-.0001'),'gt':Decimal('.0001'),'le':Decimal(0)}[relation]
        key='min_price' if relation=='gt' else 'max_price'
        if key=='max_price' and budget is not None:expected=min(expected,Decimal(str(budget)))
        source=task.get('parameter_sources',{}).get(key,{})
        p=task['parameters'];checks={'same_task':task['task_id']==old['task_id'],'task_finished':task['status'] in ('completed','empty'),
            'category_and_count_preserved':p['product_category']==old['parameters']['product_category'] and p['limit']==old['parameters']['limit'],
            'purpose_preserved':'周末' in (task['query']+' '+str(p.get('keyword',''))),
            'program_derived_bound':p.get(key) is not None and Decimal(str(p[key]))==expected,
            'anchor_audited':source.get('reference_product_id')==pid and source.get('verified_by')=='displayed_tool_price',
            'no_sort_clarification':'需要确认排序字段' not in row['answer'],'answer_present':bool(row['answer']),
            'selected_ids_exist':len(selected)==len(ids),
            'selected_prices_and_category':all(s['product_subcategory_id'] in (1,2,3) and
                (Decimal(str(s['list_price']))<price if relation=='lt' else Decimal(str(s['list_price']))>price if relation=='gt' else Decimal(str(s['list_price']))<=price)
                and (budget is None or Decimal(str(s['list_price']))<=Decimal(str(budget))) for s in selected),
            'no_private_or_write_tools':all(t['name'] in {'search_products','query_products','get_product_info','calculate_price','check_promotion'} for t in row['tools'])}
        if budget is not None and relation=='gt':checks['old_budget_retained']=p['max_price']==budget
        return checks,anchor,selected
    print('REPORT_DIR',directory,flush=True);save()
    specs=[('R01',True,None,'有没有比这个更便宜的','lt'),
           ('R02',False,None,'有没有比这个更便宜的','lt'),
           ('R03',False,1000,'有没有比这个更贵一点的','gt'),
           ('R04',False,None,'有没有不比这个贵的','le'),
           ('R05',True,500,'有没有比这个更便宜的，预算500以内','lt')]
    try:
        for cid,frozen,budget,text,relation in specs:
            with CaseFixture('owner') as fixture:
                turns=[]
                if frozen:
                    project.app.update_state({'configurable':{'thread_id':fixture.thread_id}},
                        {**identity_guard_node({'messages':[]},{'configurable':{'thread_id':fixture.thread_id}}),
                         'messages':messages_from_dict(snapshot['messages']),'tasks':snapshot['tasks']})
                    old=snapshot['tasks'][0]
                else:
                    first=turn('推荐一款适合周末骑行的自行车整车。'+('预算1000以内。' if budget else ''),fixture)
                    turns.append(first);old=product_task(first)
                result=turn(text,fixture);turns.append(result)
                checks,anchor,selected=verify(old,result,relation,budget)
                row={'id':cid,'turns':turns,'checks':checks,'anchor_sql':anchor,'selected_sql':selected,'passed':all(checks.values())}
                report['results'].append(row);save();print(cid,row['passed'],[k for k,v in checks.items() if not v],flush=True)
        # The same genuine source task, now containing two displayed catalog IDs.
        from langchain_core.messages import AIMessage,ToolMessage,HumanMessage
        with CaseFixture('owner') as fixture:
            old=json.loads(json.dumps(snapshot['tasks'][0]));old['displayed_product_ids']=[959,760];old['evidence_call_ids']=['ambiguous-source']
            with connect_database() as conn:rows=conn.execute('SELECT product_id,name,list_price,product_subcategory_id FROM product_catalog WHERE product_id=ANY(%s)',([959,760],)).fetchall()
            messages=[HumanMessage(content='推荐两款自行车'),AIMessage(content='',tool_calls=[{'id':'ambiguous-source','name':'search_products','args':{'query':'自行车'}}]),
                ToolMessage(name='search_products',tool_call_id='ambiguous-source',content=json.dumps({'success':True,'products':rows},default=str)),AIMessage(content='商品959和商品760已列出，请选择。')]
            project.app.update_state({'configurable':{'thread_id':fixture.thread_id}},
                {**identity_guard_node({'messages':[]},{'configurable':{'thread_id':fixture.thread_id}}),'messages':messages,'tasks':[old]})
            row=turn('有没有比这个更便宜的',fixture)
            from backend.tasks.task_state import ConversationTask
            checks={'no_tool_execution':not row['tools'],'clarification_present':reference_question(row['answer'],row['active_ids']),
                'old_task_preserved':next(t for t in row['tasks'] if t['task_id']==old['task_id'])==ConversationTask.model_validate(old).model_dump(mode='json')}
            report['results'].append({'id':'R06','turns':[row],'checks':checks,'passed':all(checks.values())});save();print('R06',all(checks.values()),flush=True)
    finally:
        report['source_after']=source_fingerprint();report['source_unchanged']=report['source_before']==report['source_after']
        report['passed']=len(report['results'])==6 and all(r['passed'] for r in report['results']) and report['source_unchanged'];save()
        from backend.observability.langsmith_tracing import flush_traces
        flush_traces()
    print(json.dumps({'passed':report['passed'],'cases':len(report['results'])}),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
