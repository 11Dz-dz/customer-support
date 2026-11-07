"""Check the judge against fixed answers, without calling the support Agent or business tools."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
from uuid import uuid4

from evals.judge_reference import (ROOT,load_reference,reference_manifest,render_review,
                                 public_judge_inputs,classification_metrics)
from evals.order_answer_facts import answer_fact_checks


def save(directory,report):
    report['metrics']=classification_metrics(report['results'])
    models=[m for row in report['results'] for m in row['result'].get('models',[])]
    known=[m for m in models if m.get('total_tokens') is not None]
    report['usage']={'judge_calls':len(models),'known_usage_calls':len(known),
        'reported_total_tokens':sum(m['total_tokens'] for m in known) if known else None}
    by_case={}
    for row in report['results']:
        by_case.setdefault(row['id'],[]).append(row['result'].get('score'))
    report['inconsistent_cases']=[key for key,values in by_case.items() if len({v for v in values if type(v) is bool})>1]
    (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    lines=['# 评审模型参考集验证','',
        '分数表示与Codex编写的待复核标签的一致性，不是客服准确率，也不是独立人工一致率。','',
        f"- 完成：{len(report['results'])}/{report['planned_attempts']}；状态：{report['status']}。",
        f"- 指标：{report['metrics']}。",f"- 重复判定不一致：{report['inconsistent_cases']}。",f"- 评审用量：{report['usage']}。",'',
        '## 分歧与未评分','']
    for row in report['results']:
        if row['result']['status']!='scored' or row['result']['score'] is not row['expected']:
            lines.extend([f"### {row['id']} · 第{row['repeat']}次",'',f"预期：{row['expected']}；模型：{row['result'].get('score')}。",'',
                row['reason'],'',row['result'].get('reasoning') or row['result'].get('error_type','未评分'),''])
    (directory/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    for name in ['report.json','summary.md']:(directory/name).chmod(0o600)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='真实调用DeepSeek评审，消耗额度；不重跑客服Agent')
    parser.add_argument('--repeat',type=int,default=1)
    parser.add_argument('--output-root',type=Path,default=ROOT.parent/'reports_judge_reference')
    parser.add_argument('--render-review',action='store_true',help='生成可读复核文件，不调用API')
    args=parser.parse_args(argv)
    if not 1<=args.repeat<=5:parser.error('repeat must be 1..5')
    fixture,dataset=load_reference()
    refs={r['order_id']:r for r in fixture['orders'] if r['customer_id']==fixture['customer_id']}
    rules=[]
    for case in dataset['cases']:
        if 'rule_expected' not in case:continue
        required=case.get('required',[])
        targets={r['order_id'] for r in required}
        result=answer_fact_checks(case['answer'],refs,required,
                                 default_order=next(iter(targets)) if len(targets)==1 else None)
        passed=all(c['passed'] for c in result)
        rules.append({'id':case['id'],'expected':case['rule_expected'],'actual':passed,'matched':passed is case['rule_expected']})
    if args.render_review:(ROOT/'REVIEW.md').write_text(render_review(),encoding='utf-8')
    if not all(r['matched'] for r in rules):raise ValueError('Deterministic reference checks failed: '+str(rules))
    if not args.live:
        print(json.dumps({'reference':reference_manifest(),'rule_checks':rules,'api_calls':0},ensure_ascii=False));return 0
    from backend.postgres_config import load_project_env
    load_project_env()
    from evals.frameworks import AnswerJudge
    judge=AnswerJudge()
    batch=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
    directory=args.output_root.resolve()/batch;directory.mkdir(parents=True,exist_ok=False);directory.chmod(0o700)
    report={'run_id':batch,'manifest':reference_manifest(),'judge_config':{
        'model':judge.model.model_name,'thinking':'disabled','streaming':False,'max_tokens':2048,'wall_seconds':60},
        'planned_attempts':len(dataset['cases'])*args.repeat,'results':[],'rule_checks':rules,'status':'running'}
    import hashlib
    report['manifest']['judge_adapter_sha256']=hashlib.sha256(Path(__file__).with_name('frameworks.py').read_bytes()).hexdigest()
    # Store the exact inputs alongside their hashes so future edits cannot make
    # the original judgement impossible to reconstruct.
    for name in ['cases.json','fixtures.json']:
        snapshot=directory/('reference_'+name)
        snapshot.write_bytes((ROOT/name).read_bytes());snapshot.chmod(0o600)
    print('REPORT_DIR '+str(directory),flush=True)
    try:
        for repeat in range(1,args.repeat+1):
            for case in dataset['cases']:
                result=judge.score(*public_judge_inputs(case,fixture))
                row={'id':case['id'],'category':case['category'],'repeat':repeat,'expected':case['expected'],
                     'reason':case['reason'],'result':result}
                report['results'].append(row);save(directory,report)
                print(json.dumps({'id':case['id'],'repeat':repeat,'expected':case['expected'],
                    'actual':result.get('score'),'status':result['status']},ensure_ascii=False),flush=True)
                if result.get('provider_status_code') in {401,402,403}:
                    report['status']='provider_unavailable';break
            if report['status']!='running':break
        if report['status']=='running':report['status']='completed'
    except BaseException:
        report['status']='interrupted';raise
    finally:save(directory,report)
    print(json.dumps({'report':str(directory/'summary.md'),'metrics':report['metrics']},ensure_ascii=False),flush=True)
    return 0 if report['status']=='completed' and report['metrics']['false_accepts']==0 and report['metrics']['false_rejects']==0 and report['metrics']['unavailable']==0 else 1


if __name__=='__main__':raise SystemExit(main())
