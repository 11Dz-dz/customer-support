"""Real-model exact-variant queries; public catalog snapshots, isolated threads."""
import argparse
from datetime import datetime,timezone
import hashlib,json
from pathlib import Path
from uuid import uuid4


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--snapshot',type=Path);args=parser.parse_args()
    if not args.live:print('No model calls. Eleven variant, budget, context, quote and scope scenarios.');return 0
    if not args.snapshot:parser.error('--snapshot required')
    import langgraph_supervisor_1 as project
    from langchain_core.messages import messages_from_dict,AIMessage
    from session_identity import identity_guard_node
    from task_state import ConversationTask
    from chat_runtime import stream_turn
    from chat_events import configure_event_log
    from evals.fixtures import CaseFixture,source_fingerprint
    from evals.observer import EvaluationObserver
    from evals.relative_price_runner import reference_question
    from conversation_replies import REPLIES
    from postgres_config import connect_database
    root=Path(__file__).resolve().parents[1];batch=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
    directory=root/'evals/reports_variants'/batch;directory.mkdir(parents=True);directory.chmod(0o700)
    configure_event_log(directory/'events.jsonl');snapshots=json.loads(args.snapshot.read_text(encoding='utf-8'))
    report={'batch':batch,'results':[],'source_before':source_fingerprint(),
        'snapshot_sha256':hashlib.sha256(args.snapshot.read_bytes()).hexdigest(),
        'code_files':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [*root.glob('*.py'),*root.glob('prompts/*.md'),*root.glob('config/*.json')]},
        'limits':['Authored regressions, not unseen holdout','No semantic recommendation judge or pressure acceptance','Original user thread is never invoked','No repeat to select green answers']}
    def save():(directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    def seed(fixture,key='shown',ambiguous=False):
        snapshot=snapshots[key];tasks=[ConversationTask.model_validate(t).model_dump(mode='json') for t in snapshot['tasks']]
        messages=messages_from_dict(snapshot['messages'])
        if ambiguous:
            tasks[0]['displayed_product_ids']=[989,993];tasks[0]['parameters']['product_ids']=[];tasks[0]['parameters']['limit']=2
            messages.append(AIMessage(name='sales',content='商品989：Mountain-500 Black, 40。\n商品993：Mountain-500 Black, 52。'))
        project.app.update_state({'configurable':{'thread_id':fixture.thread_id}},
            {'tasks':tasks,'messages':messages,**identity_guard_node({'messages':[]},{'configurable':{'thread_id':fixture.thread_id}})})
        return tasks
    def turn(text,fixture):
        observer=EvaluationObserver(set());chunks=[]
        for event in stream_turn(project.app,text,fixture.thread_id,callbacks=[observer]):
            if event['type']=='text':chunks.append(event['text'])
        state=project.app.get_state({'configurable':{'thread_id':fixture.thread_id}}).values
        if any(m.get('provider_status_code') in (401,402,403,429) for m in observer.models):raise RuntimeError('Provider unavailable')
        return {'input':text,'answer':''.join(chunks),'tools':observer.tools,'models':observer.models,
            'tasks':state.get('tasks',[]),'active_ids':state.get('turn_task_ids',[])}
    def record(cid,turns,checks):
        row={'id':cid,'turns':turns,'checks':checks,'passed':all(checks.values())};report['results'].append(row);save();print(cid,row['passed'],[k for k,v in checks.items() if not v],flush=True)
    with connect_database() as c:
        oracle=c.execute('SELECT product_id,color,size,list_price,product_model_id FROM product_catalog WHERE product_model_id=(SELECT product_model_id FROM product_catalog WHERE product_id=989) ORDER BY product_id').fetchall()
    report['independent_model_rows']=oracle;print('REPORT_DIR',directory,flush=True);save()
    try:
        cases=[('V01','shown','那别的颜色呢',False,{984}),
               ('V02','shown','预算不变，只看符合价格条件的其他颜色',True,set()),
               ('V03','failed','这个商品有没有别的颜色',False,{984}),
               ('V04','shown','预算600以内，看看别的颜色',True,{984}),
               ('V05','shown','那其他尺寸呢',False,{990,991,992,993}),
               ('V06',None,'商品989有没有其他颜色',False,{984})]
        for cid,key,text,strict,expected in cases:
            with CaseFixture('owner') as fixture:
                before=seed(fixture,key) if key else []
                row=turn(text,fixture);active=[t for t in row['tasks'] if t['task_id'] in row['active_ids']]
                task=next((t for t in active if t['kind']=='product_variants'),{})
                ids=set(task.get('displayed_product_ids',[]));by_id={p['product_id']:p for p in oracle}
                checks={'variant_task':bool(task),'strict_mode':task.get('parameters',{}).get('variant_strict')==strict,
                    'exact_displayed_ids':ids==expected,'ids_match_sql':all(pid in by_id for pid in ids),
                    'one_direct_variant_tool':[t['name'] for t in row['tools']]==['query_product_variants'],
                    'no_specialist_model':bool(row['models']) and all(m['role']=='router' for m in row['models']),
                    'source_tasks_unchanged':all(next(t for t in row['tasks'] if t['task_id']==old['task_id'])==old for old in before),
                    'answer_present':bool(row['answer'])}
                if cid in {'V01','V03'}:checks['outside_price_explained']='不符合原价格条件' in row['answer'] and '564.9900' in row['answer'] and '25.0000' in row['answer']
                if cid=='V02':checks['strict_explanation']='不符合当前价格条件' in row['answer'] and '商品984' not in row['answer']
                record(cid,[row],checks)
        with CaseFixture('owner') as fixture:
            seed(fixture);first=turn('预算不变，其他颜色呢',fixture);second=turn('预算提高到600，其他颜色呢',fixture)
            task=next((t for t in second['tasks'] if t['task_id'] in second['active_ids'] and t['kind']=='product_variants'),{})
            record('V07',[first,second],{'source_anchor_retained':task.get('parameters',{}).get('variant_anchor_id')==989,
                'new_budget':task.get('parameters',{}).get('max_price')==600,'silver_now_eligible':task.get('displayed_product_ids')==[984]})
        with CaseFixture('owner') as fixture:
            seed(fixture);first=turn('那别的颜色呢',fixture);second=turn('按刚才的银色款买两件，算不含税和运费的标价小计',fixture)
            quote=next((t for t in second['tools'] if t['name']=='calculate_price'),None)
            record('V08',[first,second],{'silver_displayed':'商品984' in first['answer'],'quote_tool_present':quote is not None,
                'silver_quote_amount':'1129.98' in second['answer'],
                'quote_selected_variant':bool(quote) and '984' in json.dumps(quote['args']) and '989' not in json.dumps(quote['args'])})
        with CaseFixture('owner') as fixture:
            seed(fixture);row=turn('帮我写一个查询商品其他颜色的Python程序',fixture)
            record('V09',[row],{'fixed_scope_refusal':row['answer']==REPLIES['out_of_scope'],'no_tools':not row['tools']})
        with CaseFixture('owner') as fixture:
            before=seed(fixture,ambiguous=True);row=turn('那别的颜色呢',fixture)
            record('V10',[row],{'clarifies_reference':reference_question(row['answer'],row['active_ids']),'no_tools':not row['tools'],
                'source_unchanged':all(next(t for t in row['tasks'] if t['task_id']==old['task_id'])==old for old in before)})
        with CaseFixture('owner') as fixture:
            row=turn('商品989有其他颜色吗，再查订单'+str(fixture.slots['own_order'])+'的应付金额',fixture)
            record('V11',[row],{'variant_and_order':{t['name'] for t in row['tools']}=={'query_product_variants','query_order'},
                'silver_displayed':'商品984' in row['answer'],'order_amount':'订单应付总额' in row['answer'] and '200' in row['answer']})
    finally:
        report['source_after']=source_fingerprint();report['source_unchanged']=report['source_before']==report['source_after']
        report['passed']=len(report['results'])==11 and all(r['passed'] for r in report['results']) and report['source_unchanged'];save()
        from langsmith_tracing import flush_traces
        flush_traces()
    print(json.dumps({'passed':report['passed'],'scenarios':len(report['results'])}),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
