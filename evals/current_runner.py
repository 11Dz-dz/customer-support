import argparse
from datetime import datetime,timezone
from importlib.metadata import version
import json
from pathlib import Path
from uuid import uuid4

from evals.current_dataset import VERSION,current_cases,dataset_hash,validate
SCORER_VERSION='2.4.0'


def validate_recorded_contracts(recorded,recorded_hash,refresh_contracts):
    if dataset_hash(recorded)!=recorded_hash:raise ValueError('Recorded dataset does not match its manifest')
    current=current_cases()
    if recorded==current:return
    normalized=json.loads(json.dumps(recorded))
    for case in normalized:
        if case['id'] in {'P04','P07','P08','P09'}:
            for turn in case['turns']:turn.get('contract',{}).pop('no_tools',None)
    if not refresh_contracts or normalized!=current:
        raise ValueError('Different contract; only the explicit refund-guidance migration is accepted')


def rejudge_turn(trace,spec,context,evidence,judge):
    """Preserve every prior attempt; rejudge the same answer once, even if negative."""
    import copy
    result=copy.deepcopy(trace)
    if 'judge' in result:result.setdefault('prior_judgements',[]).append(result['judge'])
    result['judge_evidence']=json.loads(json.dumps(evidence,ensure_ascii=False,default=str))
    result['judge']=judge.score(result,context,result['judge_evidence'],
        {'rules':spec['checks'],'contract':spec['contract']})
    return result


def rejudge_report(path,output_root,refresh_contracts=False):
    """Judge recorded answers, never replay target LLM calls or business tools."""
    from evals.frameworks import AnswerJudge
    from evals.calibration import calibrate
    from evals.fixtures import source_fingerprint
    from evals.current_fixture import CurrentFixture
    from evals.current_contracts import judge_evidence
    from evals.reporting import write_json
    from evals.dataset import digest
    from evals.grading import score_turn
    from evals.frameworks import trajectory_checks
    from evals.runtime_checks import execution_checks
    from evals.current_contracts import score_current,check
    from evals.dataset import load_gold
    from backend.conversation.conversation_persistence import close_persistence
    cases={c['id']:c for c in current_cases()}
    source=json.loads(path.read_text(encoding='utf-8'))
    recorded=json.loads(path.with_name('dataset.json').read_text(encoding='utf-8'))
    # Inputs, actors and expected facts must be identical; no silent test replacement.
    validate_recorded_contracts(recorded,source['manifest']['dataset_sha256'],refresh_contracts)
    if source.get('source_unchanged') is not True:
        raise ValueError('Original execution did not finish its source verification')
    if len(source['results'])!=len(source['manifest']['selected_ids'])*source['repeat'] or any(
            len(r['turns'])!=len(cases[r['id']]['turns']) for r in source['results']):
        raise ValueError('Incomplete target execution; retain it, do not disguise rejudging as completion')
    before=source_fingerprint()
    if before!=source['manifest']['source_fingerprint']:
        raise ValueError('Source data changed; cannot recreate missing SQL references')
    report=json.loads(json.dumps(source));report['original_run_id']=source.get('original_run_id',source['run_id'])
    report['run_id']=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-rejudge-'+uuid4().hex[:8]
    report['answer_rejudge']={'source_report':str(path.resolve()),'source_sha256':digest(path),
        'target_calls':0,'tool_executions':0,'status':'running','version':SCORER_VERSION,
        'prompt_sha256':digest(Path(__file__).with_name('frameworks.py')),
        'scorer_files':{name:digest(Path(__file__).with_name(name)) for name in
                        ['current_runner.py','current_contracts.py','current_dataset.py','frameworks.py']},
        'config':{'model':source['manifest']['model']['name'],'thinking':'disabled','streaming':False,
                  'max_output_tokens':2048,'wall_timeout_seconds':60},
        'note':'Original target code, rule checks, traces, answers and target usage are preserved.'}
    if refresh_contracts:
        report['original_execution_manifest']=json.loads(json.dumps(source['manifest']))
        report['manifest'].update(dataset_version=VERSION,dataset_sha256=dataset_hash(list(cases.values())),
                                  scorer_version=SCORER_VERSION)
        report['answer_rejudge'].update(refresh_contracts=True,rule_rescore_changes=[],
            note='Same recorded target execution; current rules recomputed against unchanged SQL data. Prior scores preserved.')
    report['manifest']['judge']={**report['answer_rejudge']['config'],'independent_model':False}
    directory=output_root.resolve()/report['run_id'];directory.mkdir(parents=True,exist_ok=False);directory.chmod(0o700)
    print('REPORT_DIR '+str(directory),flush=True)
    judge=AnswerJudge();gold=load_gold() if refresh_contracts else None
    try:
        report['rejudge_calibration']=calibrate(judge)
        write_json(directory/'rejudge_calibration.json',report['rejudge_calibration'])
        if not report['rejudge_calibration']['passed']:
            report['answer_rejudge']['status']='calibration_failed'
            write_json(directory/'rejudge_aborted.json',report)
            return 1
        # Do not select only previous failures. Every answer in this fixed batch is rejudged.
        for result in report['results']:
            if result.get('unavailable'):continue
            history=[];context=[];case=cases[result['id']]
            with CurrentFixture(case) as fixture:
                for index,trace in enumerate(result['turns']):
                    history.extend(trace['tools'])
                    if refresh_contracts:
                        import copy
                        previous=copy.deepcopy(trace['score'])
                        trace.setdefault('prior_rule_scores',[]).append(previous)
                        spec=case['turns'][index]
                        score=score_turn(spec,trace,history,fixture,gold)
                        checks=score['checks']
                        checks.extend(score_current(spec,trace,fixture,history))
                        checks.extend(trajectory_checks(trace,spec,history))
                        checks.extend(execution_checks(trace))
                        checks.append(check('runtime_success',trace.get('error_type') is None,dimension='runtime'))
                        # Preserve the observed original write check; a fresh fixture cannot prove past effects.
                        checks.extend(c for c in previous['checks'] if c['name']=='isolated_business_unchanged')
                        from evals.efficiency import order_detail_warning
                        checks[:]=order_detail_warning(checks,trace,spec)
                        score['auto_pass']=all(c['passed'] for c in checks)
                        score['critical_failure']=any(c['critical'] and not c['passed'] for c in checks)
                        trace['score']=score
                        if previous['auto_pass']!=score['auto_pass']:
                            report['answer_rejudge']['rule_rescore_changes'].append({'case':result['id'],'turn':index+1,
                                'before_failures':[c['name'] for c in previous['checks'] if not c['passed']],
                                'after_failures':[c['name'] for c in checks if not c['passed']]})
                    evidence=trace.get('judge_evidence') or judge_evidence(fixture,history)
                    updated=rejudge_turn(trace,case['turns'][index],context,evidence,judge)
                    updated['judge_batch_id']=report['run_id']
                    result['turns'][index]=updated
                    context.extend([{'role':'user','content':trace['input']},{'role':'assistant','content':trace['answer']}])
                    if updated['judge'].get('provider_status_code') in {401,402,403}:
                        report['answer_rejudge']['status']='provider_unavailable'
                        break
            save_report(directory,report)
            print(json.dumps({'rejudged':result['id'],'scores':[t['judge'].get('score') for t in result['turns']]}),flush=True)
            if report['answer_rejudge']['status']=='provider_unavailable':break
            if refresh_contracts:
                result['auto_pass']=bool(not result.get('error_type') and not result.get('unavailable') and
                    all(t['score']['auto_pass'] for t in result['turns']))
                result['critical_failure']=any(t['score']['critical_failure'] for t in result['turns'])
        if report['answer_rejudge']['status']=='running':report['answer_rejudge']['status']='completed'
    finally:
        report['answer_rejudge']['source_unchanged']=source_fingerprint()==before
        if not report['answer_rejudge']['source_unchanged']:report['source_unchanged']=False
        close_persistence()
        write_json(directory/'manifest.json',report['manifest'])
        write_json(directory/'dataset.json',list(cases.values()))
        save_report(directory,report)
    print(json.dumps({'report':str(directory/'summary.md'),'rule_gate':report['rule_gate_pass'],
        'quality':report['quality'],'gate_pass':report['gate_pass']},ensure_ascii=False),flush=True)
    return 0 if report['gate_pass'] else 1


def rescore_trajectory(path,output_root):
    """Re-evaluate a scorer bug against frozen observations, without replaying business operations."""
    from evals.frameworks import trajectory_checks
    from evals.reporting import write_json
    from evals.dataset import digest
    source=json.loads(path.read_text(encoding='utf-8'))
    cases={c['id']:c for c in current_cases()}
    if source['manifest']['dataset_sha256']!=dataset_hash(list(cases.values())):
        raise ValueError('Dataset differs; cannot silently rescore a different contract')
    report=json.loads(json.dumps(source));report['original_run_id']=source.get('original_run_id',source['run_id'])
    report['run_id']=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-rescore-'+uuid4().hex[:8]
    report['manifest']['scorer_version']=SCORER_VERSION
    report['trajectory_rescore']={'source_report':str(path.resolve()),'source_report_sha256':digest(path),
        'api_calls':0,'database_calls':0,'reason':'Allow-list is a set of names; invocation budget is checked separately.',
        'scorer_files':{name:digest(Path(__file__).with_name(name)) for name in ['frameworks.py','current_runner.py']},
        'changes':[],'note':'Target, judge answers and their usage were recorded in the original execution, not rerun.'}
    for result in report['results']:
        if result.get('unavailable'):continue
        history=[]
        for index,turn in enumerate(result['turns']):
            history.extend(turn['tools'])
            score=turn['score'];old=[c for c in score['checks'] if c['dimension']=='trajectory']
            updated=trajectory_checks(turn,cases[result['id']]['turns'][index],history)
            score['checks']=[c for c in score['checks'] if c['dimension']!='trajectory']+updated
            from evals.efficiency import order_detail_warning
            score['checks']=order_detail_warning(score['checks'],turn,cases[result['id']]['turns'][index])
            score['auto_pass']=all(c['passed'] for c in score['checks'])
            score['critical_failure']=any(c['critical'] and not c['passed'] for c in score['checks'])
            if [(c['name'],c['passed']) for c in old]!=[(c['name'],c['passed']) for c in updated]:
                report['trajectory_rescore']['changes'].append({'case':result['id'],'repeat':result['repeat'],
                    'turn':index+1,'before':old,'after':updated})
        result['auto_pass']=bool(not result.get('error_type') and len(result['turns'])==len(cases[result['id']]['turns'])
            and all(t['score']['auto_pass'] for t in result['turns']))
        result['critical_failure']=any(t['score']['critical_failure'] for t in result['turns'])
    directory=output_root.resolve()/report['run_id'];directory.mkdir(parents=True,exist_ok=False);directory.chmod(0o700)
    write_json(directory/'manifest.json',report['manifest']);write_json(directory/'dataset.json',list(cases.values()))
    save_report(directory,report)
    print(json.dumps({'report':str(directory/'summary.md'),'rescored_cases':len(report['results']),
        'changes':len(report['trajectory_rescore']['changes']),'api_calls':0,'gate_pass':report['gate_pass']}),flush=True)
    return 0 if report['gate_pass'] else 1


def run_case(app,case,repetition,batch_id,gold,judge):
    from evals.current_fixture import CurrentFixture
    from evals.current_contracts import score_current,judge_evidence,check
    from evals.frameworks import trajectory_checks
    from evals.grading import score_turn
    from evals.runner import run_turn
    from evals.runtime_checks import execution_checks
    from backend.observability.langsmith_tracing import request_metadata_scope
    record={'id':case['id'],'category':case['category'],'split':case['split'],'repeat':repetition,
            'turns':[],'auto_pass':False,'critical_failure':False,'unavailable':False,'error_type':None}
    history=[];context=[]
    try:
        with CurrentFixture(case) as fixture:
            record['fixture']={'thread_id':fixture.thread_id,'schema':fixture.schema,'slots':fixture.slots}
            for index,spec in enumerate(case['turns'],1):
                with request_metadata_scope(entrypoint='evaluation',eval_batch_id=batch_id,eval_case_id=case['id'],
                        eval_repeat=repetition,eval_turn=index,eval_dataset_version=VERSION):
                    trace=run_turn(app,spec,fixture,history)
                history.extend(trace['tools'])
                if trace['unavailable']:
                    trace['score']={'checks':[],'retrieval':[],'auto_pass':False,'critical_failure':False}
                    record['unavailable']=True;record['turns'].append(trace);break
                trace['score']=score_turn(spec,trace,history,fixture,gold)
                checks=trace['score']['checks']
                checks.extend(score_current(spec,trace,fixture,history))
                checks.extend(trajectory_checks(trace,spec,history))
                checks.extend(execution_checks(trace))
                checks.append(check('runtime_success',trace['error_type'] is None,trace['error_type'],'runtime'))
                checks.append(check('isolated_business_unchanged',fixture.snapshot()==fixture.before,
                                    dimension='safety',critical=True))
                from evals.efficiency import order_detail_warning
                checks[:]=order_detail_warning(checks,trace,spec)
                trace['score']['auto_pass']=all(c['passed'] for c in checks)
                trace['score']['critical_failure']=any(c['critical'] and not c['passed'] for c in checks)
                if judge:
                    # JSON-ready independent Decimal/date evidence before OpenEvals stringification.
                    evidence=json.loads(json.dumps(judge_evidence(fixture,history),ensure_ascii=False,default=str))
                    trace['judge_evidence']=evidence
                    trace['judge']=judge.score(trace,context,evidence,{'rules':spec['checks'],'contract':spec['contract']})
                else:trace['judge']={'status':'skipped','score':None,'models':[]}
                context.extend([{'role':'user','content':trace['input']},{'role':'assistant','content':trace['answer']}])
                record['turns'].append(trace)
                if trace['error_type']:
                    record['error_type']=trace['error_type'];break
                if trace['judge'].get('provider_status_code') in {401,402,403}:
                    record['judge_unavailable']=True;break
            record['auto_pass']=bool(len(record['turns'])==len(case['turns']) and not record['unavailable'] and
                all(t['score']['auto_pass'] for t in record['turns']))
            record['critical_failure']=any(t['score']['critical_failure'] for t in record['turns'])
    except Exception as exc:
        # Preparation/cleanup failures are explicit; never turn them into a quality score.
        record.update(error_type=type(exc).__name__,critical_failure=True,auto_pass=False)
    return record


def save_report(directory,report):
    from evals.reporting import write_report,write_json,percentile
    write_report(directory,report)
    turns=[t for r in report['results'] for t in r['turns']]
    judges=[t['judge'] for t in turns if 'judge' in t]
    scored=[j for j in judges if j['status']=='scored']
    models=[m for j in judges for m in j['models']]
    known=[m for m in models if m.get('total_tokens') is not None]
    report['quality']={'scored_turns':len(scored),'attempted_turns':len(judges),
        'passed_turns':sum(j['score'] is True for j in scored),
        'pass_rate':sum(j['score'] is True for j in scored)/len(scored) if scored else None,
        'coverage':len(scored)/len(turns) if turns else 0,
        'models':{'calls':len(models),'usage_reported_calls':len(known),
            'reported_total_tokens':sum(m['total_tokens'] for m in known) if known else None},
        'latency_p95_ms':percentile([j.get('elapsed_ms') for j in judges],.95),
        'independence':'同一 DeepSeek 模型评审，有同源偏差；不替代人工验收。'}
    report['rule_gate_pass']=report.pop('gate_pass')
    report['quality_gate_pass']=bool(turns and len(scored)==len(turns) and all(j['score'] is True for j in scored))
    if report.get('answer_rejudge'):
        coverage=sum(t.get('judge_batch_id')==report['run_id'] for t in turns)
        report['answer_rejudge']['rejudged_turns']=coverage
        report['quality_gate_pass'] &= report['answer_rejudge']['status']=='completed' and coverage==len(turns)
    report['gate_pass']=report['rule_gate_pass'] and report['quality_gate_pass']
    links=[{'batch_id':report.get('original_run_id',report['run_id']),'case_id':r['id'],'repeat':r['repeat'],'turn':index,
        'thread_id':r['fixture']['thread_id'],'request_id':t.get('request_id'),'trace_id':t.get('langsmith_trace_id'),
        'rule_pass':t.get('score',{}).get('auto_pass'),'judge_score':t.get('judge',{}).get('score')}
        for r in report['results'] if 'fixture' in r for index,t in enumerate(r['turns'],1)]
    write_json(directory/'trace_links.json',links)
    write_json(directory/'report.json',report)
    with (directory/'summary.md').open('a',encoding='utf-8') as f:
        f.write('\n## 当前版三层验收\n\n'+
            f"- 规则和轨迹门禁：{report['rule_gate_pass']}；回答评分门禁：{report['quality_gate_pass']}；合并门禁：{report['gate_pass']}。\n"+
            f"- OpenEvals：{report['quality']}。\n"+
            '- AgentEvals检查实际执行工具及必要筛选参数，允许等价路径；程序安排的调用另标origin=program。\n'+
            '- LangSmith关联见trace_links.json；trace_id为空表示未启用或未建立追踪，不能声称云端已收到。\n'+
            '- 当前Tracing Only密钥只写链路，未发布LangSmith Dataset/Experiment。所有分数保存在本地报告。\n'+
            '- 80个场景是人工编写的回归集，不是未见过的生产样本；不含并发压测、真实支付、完整政策知识库。\n')
        if report.get('trajectory_rescore'):
            f.write('\n本报告只对原始已录制轨迹重评允许工具集合；没有重新调用Agent、评审模型或数据库。'+
                '原始问题、回答、质量评分及消耗保留，逐项改判记录在trajectory_rescore.changes，原报告未覆盖。\n')
        if report.get('answer_rejudge'):
            f.write('\n本报告对同一批已录制回答全部重新评审，未重放Agent或工具。'+
                '原评审保存在prior_judgements；原执行配置见manifest，新评审配置及来源见answer_rejudge。'+
                '本报告的Agent用量来自原执行，新评审用量另计，不可把它们说成同一次新执行。\n')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--live',action='store_true');mode.add_argument('--validate',action='store_true')
    parser.add_argument('--ids');parser.add_argument('--smoke',action='store_true')
    parser.add_argument('--judge-report',type=Path,help='重新评审完整的已录制回答，不重放Agent/工具；需--live')
    parser.add_argument('--refresh-contracts',action='store_true',help='重评时显式应用当前规则；只允许有记录的退款指引迁移')
    mode.add_argument('--rescore-trajectory',type=Path,help='只重评已录制工具轨迹，另存报告，不重新调用Agent/评审/数据库')
    parser.add_argument('--repeat',type=int,default=1);parser.add_argument('--no-judge',action='store_true')
    parser.add_argument('--output-root',type=Path,default=Path(__file__).with_name('reports_current'))
    args=parser.parse_args(argv)
    if args.rescore_trajectory:
        return rescore_trajectory(args.rescore_trajectory,args.output_root)
    if args.judge_report:
        if not args.live:parser.error('--judge-report calls the judge API; pass --live explicitly')
        return rejudge_report(args.judge_report,args.output_root,args.refresh_contracts)
    if not 1<=args.repeat<=10:parser.error('repeat must be 1..10')
    all_cases=validate(current_cases());selected=[c for c in all_cases if not args.smoke or c['smoke']]
    if args.ids:
        ids=set(args.ids.split(','));selected=[c for c in selected if c['id'] in ids]
        if ids!={c['id'] for c in selected}:parser.error('Unknown or filtered case IDs')
    if not selected:parser.error('No selected cases')
    if not args.live:
        print(json.dumps({'version':VERSION,'cases':len(all_cases),'selected':len(selected),
            'turns':sum(len(c['turns']) for c in selected),'dataset_sha256':dataset_hash(all_cases),'api_calls':0}));return 0
    # Resolve optional SDKs before any fixtures/model calls. A missing SDK is not a scored failure.
    from evals.frameworks import AnswerJudge
    from agentevals.trajectory.match import create_trajectory_match_evaluator
    from openevals.llm import create_llm_as_judge
    from evals.reporting import manifest,write_json
    from evals.fixtures import source_fingerprint
    from evals.dataset import load_gold
    from backend.observability.chat_events import configure_event_log
    from backend.conversation.conversation_persistence import close_persistence
    from backend.observability.langsmith_tracing import flush_traces
    from backend.agent.langgraph_supervisor_1 import app
    batch=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
    directory=args.output_root.resolve()/batch;directory.mkdir(parents=True,exist_ok=False);directory.chmod(0o700)
    configure_event_log(directory/'events.jsonl');gold=load_gold()
    metadata=manifest(selected,source_fingerprint())
    metadata.update(dataset_version=VERSION,scorer_version=SCORER_VERSION,dataset_sha256=dataset_hash(all_cases),
        split_policy='Authored regression cases, not unseen holdout',
        judge={'enabled':not args.no_judge,'temperature':0,'response_format':'json_object',
               'model':metadata['model']['name'],'independent_model':False,'streaming':False,
               'max_output_tokens':2048,'wall_timeout_seconds':60,'thinking':'disabled'},
        evaluation_frameworks={name:version(name) for name in ['agentevals','openevals']})
    # Include web/fixtures/SDK adapter source as well as root graph files.
    root=Path(__file__).parents[1]
    from evals.dataset import digest
    metadata['code_files']={str(p.relative_to(root)):digest(p) for p in root.rglob('*.py')
        if not any(part.startswith('.') or part in {'node_modules','__pycache__'} for part in p.relative_to(root).parts)}
    import hashlib
    metadata['code_sha256']=hashlib.sha256(json.dumps(metadata['code_files'],sort_keys=True).encode()).hexdigest()
    report={'run_id':batch,'repeat':args.repeat,'manifest':metadata,'results':[],'source_unchanged':None}
    write_json(directory/'manifest.json',metadata);write_json(directory/'dataset.json',all_cases)
    print('REPORT_DIR '+str(directory),flush=True);judge=AnswerJudge() if not args.no_judge else None
    try:
        if judge:
            from evals.calibration import calibrate
            report['judge_calibration']=calibrate(judge)
            write_json(directory/'judge_calibration.json',report['judge_calibration'])
            if not report['judge_calibration']['passed']:
                report['aborted_reason']={'kind':'judge_calibration_failed'}
        for repetition in range(1,args.repeat+1):
            if report.get('aborted_reason'):break
            for case in selected:
                result=run_case(app,case,repetition,batch,gold,judge);report['results'].append(result)
                save_report(directory,report)
                print(json.dumps({'id':case['id'],'repeat':repetition,'rule_pass':result['auto_pass'],
                    'critical':result['critical_failure'],'error':result['error_type'],
                    'judge_failed':[i for i,t in enumerate(result['turns'],1) if t.get('judge',{}).get('score') is False]}),flush=True)
                if result['unavailable'] or result.get('judge_unavailable') or result['error_type']:
                    report['aborted_reason']={'case_id':case['id'],'kind':'provider_or_fixture_error'};break
            if report.get('aborted_reason'):break
    finally:
        try:
            report['source_after']=source_fingerprint()
            report['source_unchanged']=report['source_after']==metadata['source_fingerprint']
        finally:
            flush_traces();close_persistence();save_report(directory,report)
    print(json.dumps({'report':str(directory/'summary.md'),'rules':report['rule_gate_pass'],
        'quality':report['quality'],'gate_pass':report['gate_pass']},ensure_ascii=False),flush=True)
    return 0 if report['gate_pass'] else 1
