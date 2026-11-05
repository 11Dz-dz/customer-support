"""Expanded evaluation only. Never changes the target application's behavior."""
import argparse,hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
from collections import Counter
from evals.broad_cases import broad_cases,VERSION

def ready_judge_reference(evidence,trace,capabilities):
    return json.loads(json.dumps({**evidence,'independent_broad_reference':trace.get('broad_reference'),
        'catalog_capabilities':capabilities},ensure_ascii=False,default=str))

def write_plan(path,cases):
    lines=['# 扩展用户问题与验收目标','',f'版本：{VERSION}。人工编写的模拟测试，不是穷尽所有自然语言，也不是生产分布。','']
    for c in cases:
        lines += [f"## {c['id']} · {c['category']}",'',f"测试身份：{c['actor']}。订单编号占位符会替换成该题隔离数据。",'']
        for i,t in enumerate(c['turns'],1):
            lines += [f'### 第{i}轮','',t['input'],'','验收目标：'+t['contract']['broad']['expectation'],'']
            if t['contract']['broad'].get('catalog_sql'):lines += ['独立SQL条件：','', '```sql',t['contract']['broad']['catalog_sql'],'```','']
    path.write_text('\n'.join(lines),encoding='utf-8')

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--ids');parser.add_argument('--no-judge',action='store_true');parser.add_argument('--output-root',type=Path,default=Path(__file__).with_name('reports_broad_new'));args=parser.parse_args()
    all_cases=broad_cases();cases=all_cases
    if args.ids:
        selected=set(args.ids.split(','));cases=[c for c in cases if c['id'] in selected]
        if selected!={c['id'] for c in cases}:parser.error('Unknown IDs')
    from evals.current_dataset import validate,dataset_hash
    validate(cases)
    if not args.live:
        print(json.dumps({'cases':len(cases),'turns':sum(len(c['turns']) for c in cases),'categories':dict(Counter(c['category'] for c in cases)),'dataset_sha256':dataset_hash(cases),'api_calls':0},ensure_ascii=False));return 0
    import evals.current_runner as engine
    import evals.current_contracts as contracts
    from evals.broad_contracts import score_broad
    from evals.frameworks import AnswerJudge
    from evals.calibration import calibrate
    from evals.dataset import load_gold,digest
    from evals.reporting import manifest,write_json
    from evals.fixtures import source_fingerprint
    from conversation_persistence import close_persistence
    from chat_events import configure_event_log
    from langsmith_tracing import flush_traces
    from langgraph_supervisor_1 import app
    from catalog_query import capabilities
    class BroadJudge(AnswerJudge):
        def score(self,trace,context,evidence,contract):
            return super().score(trace,context,ready_judge_reference(evidence,trace,capabilities()),contract)
    # The reusable runner imports these at call time. The replacement is scoped
    # to this evaluation process and reverted even on cancellation/provider errors.
    original=contracts.score_current;contracts.score_current=score_broad;engine.VERSION=VERSION
    directory=args.output_root.resolve()/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]);directory.mkdir(parents=True,exist_ok=False);directory.chmod(0o700)
    configure_event_log(directory/'events.jsonl');metadata=manifest(cases,source_fingerprint());root=Path(__file__).resolve().parents[1]
    metadata.update(dataset_version=VERSION,dataset_sha256=dataset_hash(all_cases),scorer_version='broad-1',split_policy='Authored expansion; no production generalization claim',
        judge={'enabled':not args.no_judge,'model':metadata['model']['name'],'independent_model':False,'method':'json_mode'})
    metadata['code_files']={str(p.relative_to(root)):digest(p) for p in [*root.glob('*.py'),*root.glob('prompts/*.md'),*root.glob('config/*.json'),*root.glob('evals/*.py')]}
    metadata['code_sha256']=hashlib.sha256(json.dumps(metadata['code_files'],sort_keys=True).encode()).hexdigest()
    report={'run_id':directory.name,'repeat':1,'manifest':metadata,'results':[],'source_unchanged':None}
    def save():
        engine.save_report(directory,report)
        path=directory/'summary.md';text=path.read_text(encoding='utf-8')
        text=text.replace('80个场景是人工编写的回归集',str(len(cases))+'个场景是人工编写的扩展集')
        text+='\n本批数据集与验收目标见questions.md；两项明确标记的工具超时仅注入评测进程，不修改生产服务。\n'
        path.write_text(text,encoding='utf-8')
    write_json(directory/'manifest.json',metadata);write_json(directory/'dataset.json',all_cases);write_plan(directory/'questions.md',cases)
    print('REPORT_DIR',directory,flush=True);judge=BroadJudge() if not args.no_judge else None
    try:
        if judge:
            report['judge_calibration']=calibrate(judge);write_json(directory/'judge_calibration.json',report['judge_calibration'])
            if not report['judge_calibration']['passed']:report['aborted_reason']={'kind':'judge_calibration_failed'}
        if not report.get('aborted_reason'):
            for case in cases:
                from contextlib import nullcontext
                from unittest.mock import patch
                import langgraph_supervisor_1 as project
                fault_calls=[]
                original_order=project.order_service.get_order
                def fail_once(**kwargs):
                    fault_calls.append({'attempt':len(fault_calls)+1,'order_id':kwargs.get('order_id')})
                    if len(fault_calls)==1:raise TimeoutError('Controlled evaluation-only read timeout')
                    return original_order(**kwargs)
                fault_scope=patch.object(project.order_service,'get_order',new=fail_once) if case.get('fault')=='order_timeout_once' else nullcontext()
                with fault_scope:row=engine.run_case(app,case,1,directory.name,load_gold(),judge)
                if case.get('fault'):row['controlled_fault']={'kind':case['fault'],'calls':fault_calls,'scope':'This isolated evaluator process only; production service unchanged'}
                report['results'].append(row);save()
                print(json.dumps({'id':case['id'],'rules':row['auto_pass'],'critical':row['critical_failure'],'error':row['error_type'],
                    'failed_checks':[[c['name'] for c in t['score']['checks'] if not c['passed']] for t in row['turns']],
                    'judge_failed':[i for i,t in enumerate(row['turns'],1) if t.get('judge',{}).get('score') is False]},ensure_ascii=False),flush=True)
                if row['unavailable'] or row.get('judge_unavailable') or row['error_type']:
                    report['aborted_reason']={'case_id':case['id'],'kind':'provider_or_fixture_error'};break
    finally:
        contracts.score_current=original
        try:report['source_after']=source_fingerprint();report['source_unchanged']=report['source_after']==metadata['source_fingerprint']
        finally:flush_traces();close_persistence();save()
    print(json.dumps({'scenarios':len(report['results']),'turns':sum(len(r['turns']) for r in report['results']),'rules':report['rule_gate_pass'],'quality':report['quality'],'gate_pass':report['gate_pass']},ensure_ascii=False),flush=True)
    return 0 if report['gate_pass'] else 1

if __name__=='__main__':raise SystemExit(main())
