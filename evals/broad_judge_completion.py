"""Fill only SDK pre-send serialization failures; never replay target execution."""
import argparse,hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('report',type=Path);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    source=json.loads(args.report.read_text(encoding='utf-8'))
    pending=[(r['id'],i+1) for r in source['results'] for i,t in enumerate(r['turns']) if t['judge']['status']=='unavailable' and t['judge'].get('error_type')=='TypeError' and not t['judge'].get('models')]
    if not args.live:print(json.dumps({'pending':pending,'target_calls':0,'api_calls':0}));return 0
    assert source['source_unchanged'] and not source.get('aborted_reason')
    from evals.fixtures import source_fingerprint
    from evals.current_dataset import dataset_hash
    from evals.reporting import write_json
    from evals.frameworks import AnswerJudge
    from evals.broad_runner import ready_judge_reference
    from evals.calibration import calibrate
    from backend.catalog.catalog_query import capabilities
    from backend.agent.deepseek_model import provider_settings
    from evals.current_runner import save_report
    assert source_fingerprint()==source['manifest']['source_fingerprint']
    current=provider_settings();assert current['model']==source['manifest']['model']['model'] and current['base_url']==source['manifest']['model']['base_url']
    dataset=json.loads(args.report.with_name('dataset.json').read_text());assert dataset_hash(dataset)==source['manifest']['dataset_sha256'];cases={c['id']:c for c in dataset}
    output=args.report.parent.parent/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-judge-missing-'+uuid4().hex[:8]);output.mkdir()
    original_id=source['run_id'];source['run_id']=output.name;source['judge_completion']={'source_report':str(args.report),'source_sha256':hashlib.sha256(args.report.read_bytes()).hexdigest(),
        'original_run_id':original_id,'pending':pending,'completed':[],'target_calls':0,'tool_executions':0,'policy':'Only TypeError before any judge model callback; all existing positive and negative judgements retained',
        'adapter_sha256':hashlib.sha256(Path(__file__).with_name('broad_runner.py').read_bytes()).hexdigest()}
    write_json(output/'dataset.json',dataset);write_json(output/'manifest.json',source['manifest']);print('REPORT_DIR',output,flush=True)
    judge=AnswerJudge();calibration=calibrate(judge);write_json(output/'completion_calibration.json',calibration)
    if not calibration['passed']:raise RuntimeError('Judge calibration failed; no target replay')
    try:
        for row in source['results']:
            context=[]
            for index,turn in enumerate(row['turns']):
                if (row['id'],index+1) in pending:
                    old=turn['judge'];evidence=ready_judge_reference(turn['judge_evidence'],turn,capabilities())
                    turn.setdefault('prior_judgements',[]).append(old);turn['judge']=judge.score(turn,context,evidence,{'rules':cases[row['id']]['turns'][index]['checks'],'contract':cases[row['id']]['turns'][index]['contract']})
                    source['judge_completion']['completed'].append([row['id'],index+1]);save_report(output,source)
                    print(row['id'],index+1,turn['judge']['status'],turn['judge'].get('score'),flush=True)
                    if turn['judge'].get('provider_status_code') in {401,402,403}:raise RuntimeError('Judge provider unavailable')
                context.extend([{'role':'user','content':turn['input']},{'role':'assistant','content':turn['answer']}])
    finally:
        source['judge_completion']['source_unchanged']=source_fingerprint()==source['manifest']['source_fingerprint'];save_report(output,source)
    print(json.dumps({'completed':len(source['judge_completion']['completed']),'quality':source['quality'],'target_calls':0}),flush=True)
    return 0 if source['quality']['coverage']==1 else 1

if __name__=='__main__':raise SystemExit(main())
