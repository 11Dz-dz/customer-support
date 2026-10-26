"""Read-only business diagnostic using frozen questions and existing isolated eval fixtures."""
import argparse
from collections import Counter
from contextlib import ExitStack
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import statistics
import sys
from unittest.mock import patch


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--ids',help='Comma-separated IDs for a targeted regression')
    args=parser.parse_args()
    root=args.root.resolve()
    here=Path(__file__).resolve().parent
    manifest=json.loads((here/'routing_manifest_v2.json').read_text())
    cases_path=here/'routing_cases_v2.json'
    assert sha(cases_path)==manifest['sha256']
    cases=json.loads(cases_path.read_text())
    assert len(cases)==manifest['case_count']==24
    assert sum(len(c['turns']) for c in cases)==manifest['turn_count']==47
    assert all(t['expect']['no_write'] for c in cases for t in c['turns'])
    if not args.live:
        print(json.dumps({'validated':True,**manifest},ensure_ascii=False));return 0
    if args.ids:
        requested=set(args.ids.split(','))
        cases=[c for c in cases if c['id'] in requested]
        assert requested=={c['id'] for c in cases}
    sys.path.insert(0,str(root))
    from postgres_config import connect_database
    from evals.fixtures import CaseFixture,source_fingerprint
    from evals.runner import run_turn
    from evals.validation_runner import check_turn
    from tool_execution import READ_ONLY_TOOLS
    import langgraph_supervisor_1 as project
    from chat_events import configure_event_log
    frozen_files=[*root.glob('*.py'),*root.glob('*.sql'),*(root/'tests').glob('*.py'),
                  *(root/'evals').glob('*.py'),*(root/'evals').glob('*.json'),*(root/'evals').glob('*.jsonl')]
    hashes={p.relative_to(root).as_posix():sha(p) for p in frozen_files}
    before=source_fingerprint()
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-routing-v2-'+manifest['sha256'][:8]
    dest=root/'evals/reports'/run_id
    dest.mkdir(mode=0o700,parents=True,exist_ok=False)
    configure_event_log(dest/'events.jsonl')
    (dest/cases_path.name).write_bytes(cases_path.read_bytes())
    first=not any(p!=dest/cases_path.name for p in (root/'evals/reports').glob('*/routing_cases_v2.json'))
    report={'run_id':run_id,'dataset_manifest':manifest,'first_use':first,
            'scope':'Actual DeepSeek, simulated data; no business changes, no multiuser test, not independent human review',
            'source_before':before,'code_hashes':hashes,'probe_runner_sha256':sha(Path(__file__)),
            'results':[],'aborted_reason':None,'planned_cases':len(cases),'planned_turns':sum(len(c['turns']) for c in cases)}
    def save():
        p=dest/'routing_report.json'
        p.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
        p.chmod(0o600)
    print('REPORT_DIR',dest,flush=True)
    for case in cases:
        record={'id':case['id'],'category':case['category'],'title':case['title'],
                'turns':[],'passed':False,'critical_failure':False,'cleanup_ok':False}
        try:
            with CaseFixture(case['actor']) as fixture,ExitStack() as stack:
                if case.get('fault'):
                    fault=case['fault'];counter=[0]
                    original=project.order_service.get_payment
                    def failing_payment(*a,**kw):
                        counter[0]+=1
                        if counter[0] in fault['fail_calls']:raise TimeoutError('Controlled read-only failure')
                        return original(*a,**kw)
                    stack.enter_context(patch.object(project.order_service,'get_payment',failing_payment))
                for spec in case['turns']:
                    trace=run_turn(project.app,{'input':spec['input']},fixture,[])
                    state=project.app.get_state({'configurable':{'thread_id':fixture.thread_id}}).values
                    trace['checks']=check_turn(spec['expect'],trace,state,fixture)
                    trace['tasks']=state.get('tasks',[])
                    current=[t for t in trace['tasks'] if t['task_id'] in state.get('turn_task_ids',[])]
                    expect=spec['expect']
                    def add(name,ok,detail=None,critical=False):
                        trace['checks'].append({'name':name,'passed':bool(ok),'detail':detail,'critical':critical})
                    for kind in expect.get('require_completed_kinds',[]):
                        selected=[t for t in current if t['kind']==kind]
                        add('completed_'+kind,bool(selected) and all(t['status'] in {'completed','empty'} and not t.get('failure_code') for t in selected))
                    if 'contains_any' in expect:
                        add('clarification_present',any(s in trace['answer'] for s in expect['contains_any']))
                    if 'kind_counts' in expect:
                        add('task_kind_counts',Counter(t['kind'] for t in current)==Counter(expect['kind_counts']),[t['kind'] for t in current])
                    for kind,fields in expect.get('parameter_for_kind',{}).items():
                        selected=[t for t in current if t['kind']==kind]
                        for field,value in fields.items():
                            add('scoped_parameter_'+kind+'_'+field,len(selected)==1 and selected[0]['parameters'].get(field)==value)
                    for tool,fields in expect.get('tool_arguments',{}).items():
                        calls=[t for t in trace['tools'] if t['name']==tool]
                        add('arguments_'+tool,bool(calls) and all(any(str(t['args'].get(k))==str(v).format(**fixture.slots) for t in calls) for k,v in fields.items()),[t['args'] for t in calls])
                    if expect.get('no_foreign_data'):
                        for tool in trace['tools']:
                            if tool['name'] in {'query_order','check_payment_status','query_invoice'}:
                                value=tool.get('result')
                                if isinstance(value,dict) and (case['actor']=='guest' or str(tool['args'].get('order_id'))==str(fixture.slots['foreign_order'])):
                                    add('order_access_denied',value.get('success') is False,value.get('error_code'),True)
                    with connect_database() as conn:
                        tickets=conn.execute('SELECT count(*) AS n FROM human_handoffs WHERE thread_id=%s',(fixture.thread_id,)).fetchone()['n']
                    trace['handoff_count']=tickets
                    if expect.get('no_handoffs'):add('no_unrequested_handoff',tickets==0,tickets)
                    trace['passed']=all(c['passed'] for c in trace['checks'])
                    record['turns'].append(trace)
                    record['critical_failure'] |= any(c.get('critical') and not c['passed'] for c in trace['checks'])
                    if trace.get('unavailable'):
                        report['aborted_reason']={'kind':'provider_unavailable','status':trace.get('provider_status_code'),'case':case['id']}
                        break
                counts=fixture.refund_counts()
                record['refund_counts']=counts
                record['critical_failure'] |= any(counts.values())
            record['cleanup_ok']=True
            record['passed']=len(record['turns'])==len(case['turns']) and all(t['passed'] for t in record['turns']) and not record['critical_failure']
        except Exception as exc:
            record['error_type']=type(exc).__name__
            # Private trace records only sanitized exception classes, never credentials.
            if not record['cleanup_ok']:record['critical_failure']=True
        report['results'].append(record)
        save()
        failed=[(i,c['name']) for i,t in enumerate(record['turns'],1) for c in t['checks'] if not c['passed']]
        print('CASE',case['id'],'PASS' if record['passed'] else 'FAIL',json.dumps(failed,ensure_ascii=False),flush=True)
        if report['aborted_reason']:break
    report['source_after']=source_fingerprint()
    report['source_unchanged']=before==report['source_after']
    report['code_unchanged']=all(sha(root/n)==h for n,h in hashes.items())
    report['cases_completed']=len(report['results'])
    report['turns_completed']=sum(len(c['turns']) for c in report['results'])
    report['passed_cases']=sum(c['passed'] for c in report['results'])
    report['critical_failures']=sum(c['critical_failure'] for c in report['results'])
    report['all_checks_passed']=not report['aborted_reason'] and report['passed_cases']==len(cases) and report['source_unchanged'] and report['code_unchanged']
    models=[m for c in report['results'] for t in c['turns'] for m in t['models']]
    usage=[m for m in models if m.get('total_tokens') is not None]
    report['model_calls']=len(models)
    report['reported_tokens']=sum(m['total_tokens'] for m in usage)
    report['token_usage_coverage']=len(usage)/len(models) if models else None
    save()
    summary=['# 路由回归 v2（2026-10-24）','',
        f"完成{report['cases_completed']}/{len(cases)}场景、{report['turns_completed']}/{report['planned_turns']}轮；自动规则通过{report['passed_cases']}场景。",
        f"严重检查失败{report['critical_failures']}；源数据未变：{report['source_unchanged']}；本次运行期间代码未变：{report['code_unchanged']}。",
        f"首次使用：{first}；服务中断：{report['aborted_reason']}。",'',
        '实际调用DeepSeek，问题及业务数据均为模拟；不是独立人工或真实客户验收。本次为修复后的回归；首次测试报告保留，不覆盖。',
        '工具顺序等精确断言失败须结合原始回答区分评分约束与实际业务问题。','']
    review=['# 新增路由测试逐项记录','', '列出每轮输入、实际回答、任务与未通过原因。以下均为模拟场景。','']
    for c in report['results']:
        summary.append(f"- {c['id']} {c['title']}：{'通过' if c['passed'] else '未通过'}")
        review += [f"## {c['id']} {c['title']}",'',f"结果：{'通过' if c['passed'] else '未通过'}；清理完成：{c['cleanup_ok']}",'']
        for i,t in enumerate(c['turns'],1):
            failures=[x for x in t['checks'] if not x['passed']]
            for x in failures:summary.append(f"  - 第{i}轮：{x['name']}；{x.get('detail')}")
            review += [f'### 第{i}轮','', '**用户输入**','',t['input'],'','**实际回答**','',t['answer'],'',
                       '**实际工具**','',', '.join(x['name'] for x in t['tools']) or '无','',
                       '**未通过检查**','',*([f"- {x['name']}：{x.get('detail')}" for x in failures] or ['无']), '']
        if c.get('error_type'):summary.append('  - 执行/清理异常：'+c['error_type'])
    for name,lines in [('routing_summary.md',summary),('routing_review.md',review)]:
        p=dest/name;p.write_text('\n'.join(lines)+'\n');p.chmod(0o600)
    print(json.dumps({k:report[k] for k in ['run_id','cases_completed','turns_completed','passed_cases','critical_failures','source_unchanged','code_unchanged','model_calls','reported_tokens','token_usage_coverage','aborted_reason']},ensure_ascii=False),flush=True)
    return 0 if report['all_checks_passed'] else 1


if __name__=='__main__':raise SystemExit(main())
