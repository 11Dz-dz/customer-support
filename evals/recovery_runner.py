"""Simulated acceptance for timeout/handoff features; authored after implementation, not blind validation."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from evals.fixtures import CaseFixture, source_fingerprint
from evals.runner import run_turn
from evals.validation_runner import check_turn
from human_handoff import ensure_handoff_schema
from postgres_config import connect_database

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--output-root', type=Path, default=ROOT/'evals/reports')
    args = parser.parse_args()
    import langgraph_supervisor_1 as project
    dataset = ROOT/'evals/recovery_cases.json'
    cases = json.loads(dataset.read_text())
    dest = args.output_root/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-recovery-'+uuid4().hex[:8])
    dest.mkdir(parents=True)
    print('REPORT_DIR', dest, flush=True)
    ensure_handoff_schema()
    before = source_fingerprint()
    hashes = {str(p.relative_to(ROOT)): sha(p) for p in list(ROOT.glob('*.py'))+list((ROOT/'evals').glob('*.py'))}
    report = {'scope':'simulated feature acceptance, not independent blind/human acceptance',
              'dataset_sha256':sha(dataset), 'code_hashes':hashes, 'results':[], 'planned_cases':len(cases),
              'source_fingerprint':before}
    for case in cases:
        record = {'id':case['id'], 'turns':[], 'passed':False}
        thread_ids = []
        try:
            with CaseFixture(case['actor']) as fixture, ExitStack() as stack:
                thread_ids.append(fixture.thread_id)
                counter = [0]
                if case.get('fault'):
                    fault = case['fault']; original = getattr(project.order_service, fault['method'])
                    def faulty(*a, **kw):
                        counter[0] += 1
                        if counter[0] in fault['fail_calls']:
                            raise TimeoutError('simulated typed temporary fault')
                        return original(*a, **kw)
                    stack.enter_context(patch.object(project.order_service, fault['method'], faulty))
                for spec in case['turns']:
                    trace = run_turn(project.app, spec, fixture, [])
                    state = project.app.get_state({'configurable':{'thread_id':fixture.thread_id}}).values
                    checks = check_turn(spec['expect'], trace, state, fixture)
                    with connect_database() as conn:
                        tickets = conn.execute('SELECT ticket_id,status,reason FROM customer_service.human_handoffs '
                            'WHERE thread_id=%s', (fixture.thread_id,)).fetchall()
                    checks.append({'name':'ticket_count','passed':len(tickets)==spec['tickets'],'detail':tickets})
                    if case.get('no_model'):
                        checks.append({'name':'no_model_calls','passed':not trace['models']})
                    trace.update(checks=checks, passed=all(c['passed'] for c in checks), tasks=state.get('tasks', []))
                    record['turns'].append(trace)
                    if trace.get('unavailable'):
                        report['aborted_reason']={'kind':'provider_unavailable','http_status':trace.get('provider_status_code')}
                        break
                counts = fixture.refund_counts()
                record['no_refund_rows'] = not any(counts.values())
                record['fault_calls'] = counter[0]
                record['passed'] = (len(record['turns'])==len(case['turns']) and record['no_refund_rows']
                    and all(t['passed'] for t in record['turns'])
                    and counter[0]==case.get('expected_fault_calls', counter[0]))
        except Exception as exc:
            record['error_type'] = type(exc).__name__
        finally:
            with connect_database() as conn:
                conn.execute('DELETE FROM customer_service.human_handoffs WHERE thread_id=ANY(%s)', (thread_ids,))
        report['results'].append(record)
        (dest/'recovery_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        print('RECOVERY_CASE', case['id'], 'PASS' if record['passed'] else 'FAIL', flush=True)
        if report.get('aborted_reason'):
            break
    report.update(passed=sum(r['passed'] for r in report['results']),
                  turn_count=sum(len(r['turns']) for r in report['results']),
                  source_unchanged=before==source_fingerprint(),
                  code_unchanged=all(sha(ROOT/n)==h for n,h in hashes.items()))
    report['all_checks_passed'] = (report['passed']==len(cases) and report['source_unchanged'] and report['code_unchanged'])
    (dest/'recovery_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    summary = '# 请求超时与转人工功能验收\n\n' + f"{report['passed']}/{len(cases)}场景，{report['turn_count']}轮。\n\n"
    summary += '模拟样例、实现后编写的功能验收，不能称为独立盲测或真实客户效果。\n\n'
    for record in report['results']:
        summary += '- '+record['id']+': '+('通过' if record['passed'] else '未通过')+'\n'
        for n, turn in enumerate(record['turns'], 1):
            summary += ''.join(f"  - 第{n}轮：{c['name']}未通过。\n" for c in turn['checks'] if not c['passed'])
    (dest/'recovery_summary.md').write_text(summary)
    print(json.dumps({k:report[k] for k in ['passed','planned_cases','turn_count','source_unchanged','all_checks_passed']}),flush=True)
    return 0 if report['all_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
