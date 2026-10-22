"""Frozen compound-request regression; --live consumes DeepSeek tokens and uses isolated fixtures."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from evals.fixtures import CaseFixture, source_fingerprint


def check_trace(spec, state, tool_names, answer):
    checks = []
    def add(name, ok):
        checks.append({'name':name, 'passed':bool(ok)})
    tasks = state.get('tasks', [])
    for key, status in [('waiting','awaiting_input'),('blocked','awaiting_dependency'),('denied','denied'),('cancelled','cancelled')]:
        if key in spec:
            add(key, sum(t['status']==status for t in tasks)==spec[key])
    if 'task_count' in spec:
        add('task_count', len(tasks)==spec['task_count'])
    if 'exact_tools' in spec:
        add('exact_tools', tool_names==spec['exact_tools'])
    for name in spec.get('tools', []):
        add('has_'+name, name in tool_names)
    if 'forbidden_text' in spec:
        add('no_unauthorized_product', spec['forbidden_text'] not in answer)
    if 'derived_products' in spec:
        add('derived_products', any(t['kind']=='product_detail' and t['parameters']['product_ids']==spec['derived_products'] for t in tasks))
    if 'displayed_products' in spec:
        add('displayed_products', any(t['kind']=='product_search' and len(t.get('displayed_product_ids',[]))==spec['displayed_products'] for t in tasks))
    add('answer_present', bool(answer.strip()))
    return checks


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--ids',help='Comma-separated case IDs')
    parser.add_argument('--output-root',type=Path,default=Path(__file__).resolve().parents[1]/'evals/reports')
    args=parser.parse_args()
    if not args.live:
        parser.error('Pass --live to execute the frozen model-backed regression')
    cases_path=Path(__file__).with_name('multitask_cases.json')
    cases=json.loads(cases_path.read_text())
    if args.ids:
        requested=set(args.ids.split(','))
        cases=[c for c in cases if c['id'] in requested]
        if {c['id'] for c in cases}!=requested:
            parser.error('Unknown case ID')
    import langgraph_supervisor_1 as project
    from chat_runtime import stream_turn
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-multitask-'+uuid4().hex[:8]
    dest=args.output_root/run_id
    dest.mkdir(parents=True)
    dest.chmod(0o700)
    root=Path(__file__).resolve().parents[1]
    hashes={n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in
            ['task_state.py','task_scheduler.py','tool_execution.py','langgraph_supervisor_1.py']}
    before=source_fingerprint()
    records=[]
    for case in cases:
        record={'id':case['id'],'turns':[],'passed':False}
        try:
            with CaseFixture(case['actor']) as f:
                for spec in case['turns']:
                    config={'configurable':{'thread_id':f.thread_id}}
                    old=project.app.get_state(config).values.get('messages',[])
                    query=spec['input'].format(**f.slots)
                    parts=list(stream_turn(project.app,query,f.thread_id))
                    state=project.app.get_state(config).values
                    tools=[m.name for m in state['messages'][len(old):] if m.type=='tool']
                    answer=state['messages'][-1].content
                    checks=check_trace(spec,state,tools,answer)
                    checks.append({'name':'stream_matches_answer','passed':''.join(p['text'] for p in parts if p['type']=='text')==answer})
                    trace={'input':query,'answer':answer,'tools':tools,'tasks':state.get('tasks',[]),
                           'budget':state.get('turn_budget'),'checks':checks}
                    record['turns'].append(trace)
                counts=f.refund_counts()
                record['no_refund_side_effects']=not any(counts.values())
                record['passed']=record['no_refund_side_effects'] and all(all(c['passed'] for c in t['checks']) for t in record['turns'])
        except Exception as exc:
            record['error_type']=type(exc).__name__
        records.append(record)
        (dest/'report.json').write_text(json.dumps({'run_id':run_id,'results':records,'code_hashes':hashes},ensure_ascii=False,indent=2))
        print('MULTITASK_CASE',record['id'],'PASS' if record['passed'] else 'FAIL',flush=True)
    unchanged=before==source_fingerprint()
    code_unchanged=all(hashlib.sha256((root/n).read_bytes()).hexdigest()==value for n,value in hashes.items())
    passed=sum(r['passed'] for r in records)
    report={'run_id':run_id,'results':records,'code_hashes':hashes,'cases_sha256':hashlib.sha256(cases_path.read_bytes()).hexdigest(),
            'passed':passed,'case_count':len(records),'turn_count':sum(len(r['turns']) for r in records),
            'source_unchanged':unchanged,'code_unchanged':code_unchanged,
            'gate_pass':passed==len(records) and unchanged and code_unchanged,
            'scope':'Simulated regression, automated checks; not independent human acceptance'}
    (dest/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    text=f'# Compound request regression\n\n{passed}/{len(records)} cases passed; {report["turn_count"]} turns. Source unchanged: {unchanged}.\n\n'
    for r in records:
        text+=f'- {r["id"]}: {"PASS" if r["passed"] else "FAIL"}\n'
    text+='\nSimulated regression with automated checks; not independent human acceptance.\n'
    (dest/'summary.md').write_text(text)
    print('REPORT_DIR',dest,flush=True)
    return 0 if report['gate_pass'] else 1


if __name__=='__main__':
    raise SystemExit(main())
