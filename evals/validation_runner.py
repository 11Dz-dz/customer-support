"""Run the frozen validation set once before using any failures for development.

Records automated checks and controlled failure injection. This is simulated data authored
by the implementation assistant, not independent human review or real customer traffic.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage,HumanMessage
from adventureworks_service import AdventureWorksService
from evals.fixtures import CaseFixture,source_fingerprint
from evals.runner import run_turn
from tool_execution import READ_ONLY_TOOLS

ROOT=Path(__file__).resolve().parents[1]
FAULT_METHODS={'lookup_order':'get_order','query_invoice':'get_invoice',
               'check_payment_status':'get_payment','get_product_info':'get_product'}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_turn(expect,trace,state,fixture):
    checks=[]
    def add(name,passed,detail=None,critical=False):
        checks.append({'name':name,'passed':bool(passed),'detail':detail,'critical':critical})
    names=[t['name'] for t in trace['tools']]
    tasks={t['task_id']:t for t in state.get('tasks',[])}
    current=[tasks[key] for key in state.get('turn_task_ids',[]) if key in tasks]
    answer=trace['answer']
    if 'tools' in expect:add('tools',names==expect['tools'],names)
    for name in expect.get('at_least_tools',[]):add('tool_'+name,name in names,names)
    for name in expect.get('forbidden_tools',[]):add('no_'+name,name not in names,names,critical=name not in READ_ONLY_TOOLS)
    if 'turn_kinds' in expect:add('task_kinds',[t['kind'] for t in current]==expect['turn_kinds'],[t['kind'] for t in current])
    for field,rows in [('statuses',current),('all_statuses',list(tasks.values()))]:
        for status,count in expect.get(field,{}).items():add(field+'_'+status,sum(t['status']==status for t in rows)==count,[t['status'] for t in rows])
    for text in expect.get('contains',[]):
        value=text.format(**fixture.slots);add('contains_'+text,value in answer)
    for text in expect.get('absent',[])+expect.get('forbidden_claims',[]):add('absent_'+text,text not in answer,critical=text.startswith('商品749'))
    if expect.get('no_write'):add('no_write',all(n in READ_ONLY_TOOLS for n in names),names,True)
    if 'failed_operations' in expect:
        count=sum(o['status']!='succeeded' for t in current for o in t.get('operations',[]))
        add('failed_operations',count==expect['failed_operations'],count)
    if 'displayed_products' in expect:
        selected=[t for t in current if t['kind']=='product_search']
        add('displayed_products',bool(selected) and len(selected[0].get('displayed_product_ids',[]))==expect['displayed_products'])
        if selected:
            add('product_answer_present',all('商品'+str(pid) in answer for pid in selected[0].get('displayed_product_ids',[])))
    if expect.get('product_kind') == 'bicycle':
        ids=[pid for t in current if t['kind']=='product_search' for pid in t.get('displayed_product_ids',[])]
        facts=[AdventureWorksService().get_product(str(pid)) for pid in ids]
        add('whole_bicycles',bool(ids) and all(f.get('success') is True and
            f.get('product',{}).get('product_number','').startswith('BK-') for f in facts),ids)
    if 'parameters' in expect:
        for key,value in expect['parameters'].items():add('parameter_'+key,len(current)==1 and current[0]['parameters'].get(key)==value)
    if 'quote' in expect:
        q=expect['quote'];fact=AdventureWorksService().calculate_price(str(q['product']),q['quantity'])
        add('quote_amount',fact.get('success') is True and str(fact['subtotal']) in answer)
    if 'history_messages_min' in expect:add('full_history_retained',len(state['messages'])>=expect['history_messages_min'])
    contexts=[e for e in trace['events'] if e.get('event')=='context_prepared']
    if expect.get('context_bounded'):
        add('context_observed',bool(contexts))
        add('context_bounded',all(e['output_chars']<=e['limit_chars'] for e in contexts))
    if expect.get('expert_context_required'):add('expert_context_observed',any(e.get('role')=='specialist' for e in contexts))
    add('runtime_success',trace.get('error_type') is None,trace.get('error_type'))
    add('provider_available',not trace.get('unavailable'),trace.get('provider_status_code'))
    add('no_safety_violation',not trace.get('violations'),trace.get('violations'),True)
    add('answer_present',bool(answer.strip()))
    return checks


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--validate',action='store_true')
    parser.add_argument('--dataset', choices=['validation_v1','validation_v2'], default='validation_v1')
    parser.add_argument('--output-root',type=Path,default=ROOT/'evals/reports')
    args=parser.parse_args(argv)
    cases_path=Path(__file__).with_name(args.dataset+'.json')
    frozen=json.loads(cases_path.with_name(args.dataset+'_manifest.json').read_text())
    assert sha(cases_path)==frozen['sha256'],'Frozen validation set was modified'
    cases=json.loads(cases_path.read_text())
    assert len(cases)==frozen['case_count'] and sum(len(c['turns']) for c in cases)==frozen['turn_count']
    assert len({c['id'] for c in cases})==len(cases)
    if args.validate:
        print(json.dumps({'validated':True,**frozen},ensure_ascii=False));return 0
    if not args.live:parser.error('Pass --live to consume model tokens')
    # Find prior uses in the canonical report folder as well as the requested destination.
    previous=[]
    roots={args.output_root.resolve(),(ROOT/'evals/reports').resolve()}
    for folder in roots:
        for p in folder.glob('*/validation_report.json'):
            try:
                report=json.loads(p.read_text())
                if report.get('dataset_sha256')==frozen['sha256']:previous.append(str(p))
            except (ValueError,OSError):pass
    import langgraph_supervisor_1 as project
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-validation-'+uuid4().hex[:8]
    dest=args.output_root/run_id;dest.mkdir(parents=True);dest.chmod(0o700)
    from chat_events import configure_event_log
    configure_event_log(dest/'events.jsonl')
    code_hashes={p.name:sha(p) for p in ROOT.glob('*.py')}
    before=source_fingerprint()
    report={'run_id':run_id,'dataset_version':args.dataset,'dataset_sha256':frozen['sha256'],
            'first_live_run':not previous,'previous_runs':previous,'frozen_manifest':frozen,
            'code_hashes':code_hashes,'runner_sha256':sha(Path(__file__)),
            'scope':'Simulated automated validation, not independent human acceptance','results':[]}
    print('REPORT_DIR',dest,flush=True)
    for case in cases:
        record={'id':case['id'],'turns':[],'passed':False,'critical_failure':False}
        try:
            with CaseFixture(case['actor']) as fixture,ExitStack() as stack:
                fault=case.get('fault')
                if fault:
                    method=FAULT_METHODS[fault['method']]
                    original=getattr(project.order_service,method)
                    counter=[0]
                    def faulty(*a,**kw):
                        counter[0]+=1
                        if counter[0] in fault['fail_calls']:
                            if fault.get('return_failure'):return {'success':False,'error_code':'TEMPORARY_ERROR','error':'评测注入的临时失败'}
                            raise TimeoutError('Simulated temporary tool failure')
                        return original(*a,**kw)
                    stack.enter_context(patch.object(project.order_service,method,faulty))
                for spec in case['turns']:
                    trace=run_turn(project.app,{'input':spec['input']},fixture,[])
                    state=project.app.get_state({'configurable':{'thread_id':fixture.thread_id}}).values
                    trace['checks']=check_turn(spec['expect'],trace,state,fixture)
                    trace['tasks']=state.get('tasks',[])
                    trace['history_message_count']=len(state['messages'])
                    trace['passed']=all(c['passed'] for c in trace['checks'])
                    record['turns'].append(trace)
                    record['critical_failure'] |= any(c['critical'] and not c['passed'] for c in trace['checks'])
                    if trace.get('unavailable'):
                        report['aborted_reason']={'kind':'provider_unavailable','http_status':trace.get('provider_status_code'),'case':case['id']}
                        break
                    if spec.get('padding_after'):
                        padding=[]
                        for n in range(spec['padding_after']):
                            padding += [HumanMessage(content=f'历史延长样本{n}，没有新的业务请求。'+'会话测试。'*180),AIMessage(content='不改变当前业务任务。'+'历史记录。'*180)]
                        project.app.update_state({'configurable':{'thread_id':fixture.thread_id}},{'messages':padding})
                counts=fixture.refund_counts()
                expected=case.get('expected_refunds',0)
                record['side_effects']={'own':counts[fixture.slots['own_order']],'foreign':counts[fixture.slots['foreign_order']],'expected_own':expected}
                record['critical_failure'] |= bool(record['side_effects']['foreign'] or record['side_effects']['own']>expected)
                record['passed']=len(record['turns'])==len(case['turns']) and all(t['passed'] for t in record['turns']) and record['side_effects']['own']==expected and not record['critical_failure']
        except Exception as exc:
            record['error_type']=type(exc).__name__
        report['results'].append(record)
        (dest/'validation_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
        print('VALIDATION_CASE',case['id'],'PASS' if record['passed'] else 'FAIL',flush=True)
        if report.get('aborted_reason'):break
    report['source_unchanged']=before==source_fingerprint()
    report['code_unchanged']=all(sha(ROOT/n)==value for n,value in code_hashes.items())
    report['passed']=sum(r['passed'] for r in report['results'])
    report['case_count']=len(report['results']);report['planned_cases']=len(cases)
    report['turn_count']=sum(len(r['turns']) for r in report['results'])
    report['critical_failures']=sum(r['critical_failure'] for r in report['results'])
    report['all_checks_passed']=report['passed']==len(cases) and report['source_unchanged'] and report['code_unchanged']
    (dest/'validation_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    text=f'# 验证集实测：{args.dataset}（{"首次" if report["first_live_run"] else "回归"}）\n\n{report["passed"]}/{len(cases)}场景通过，实际{report["turn_count"]}轮。首次使用：{report["first_live_run"]}。源数据未变：{report["source_unchanged"]}。\n\n'
    for r in report['results']:
        text+=f'- {r["id"]}: {"通过" if r["passed"] else "未通过"}\n'
        for i,t in enumerate(r['turns'],1):
            for c in t['checks']:
                if not c['passed']:text+=f'  - 第{i}轮：{c["name"]}；{c.get("detail")}\n'
    text+='\n模拟数据，由开发助手在本轮实现前编写并冻结；不是独立人工验收。此结果保留，若据此修复问题，后续重跑属于回归，不再称作未参与调试的独立验证。\n'
    (dest/'validation_summary.md').write_text(text)
    print(json.dumps({k:report[k] for k in ['passed','planned_cases','turn_count','first_live_run','critical_failures','source_unchanged','all_checks_passed']}),flush=True)
    return 0 if report['all_checks_passed'] else 1


if __name__=='__main__':raise SystemExit(main())
