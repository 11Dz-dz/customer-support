"""Small real-model regression for the composition refactor; explicit --live only.

Existing case contracts are reused unchanged. Raw answers stay in local reports.
This does not score semantic recommendation quality or replace the 80-case suite.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def recorded_command(directory, item):
    """Read actual case records; --no-judge intentionally leaves the total gate red."""
    text=(directory/(item['label']+'.log')).read_text(encoding='utf-8')
    match=re.search(r'^REPORT_DIR\s+(.+)$',text,re.M)
    if not match:return {**item,'passed':False,'reason':'No case report'}
    path=Path(match[1].strip())/'report.json'
    recorded=json.loads(path.read_text(encoding='utf-8'))
    if item['label']=='existing_six':
        results=recorded['results']
        complete=(len(results)==6 and {row['id'] for row in results}=={'G01','C01','O02','P01','N01','X01'})
        passed=complete and recorded['rule_gate_pass'] and recorded['source_unchanged']
        detail={'executed_cases':len(results),'rules_passed':recorded['rule_gate_pass'],
            'original_total_gate':recorded['gate_pass'],'quality_assessed':False}
    else:
        results=recorded['end_to_end']
        complete=len(results)==1 and results[0]['id']=='E03' and len(results[0]['turns'])==2
        passed=complete and recorded['passed'] and recorded['source_unchanged']
        detail={'executed_cases':len(results),'turns':sum(len(row['turns']) for row in results)}
    return {**item,'report':str(path),'report_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'passed':bool(passed),'source_unchanged':recorded['source_unchanged'],**detail}


def review_existing(path):
    """Derive a scope-specific review without editing reports or replaying tools."""
    report=json.loads(path.read_text(encoding='utf-8'))
    commands=[recorded_command(path.parent,item) for item in report['commands']]
    passed=all(item['passed'] for item in commands) and report['comparison']['passed'] and report['source_unchanged_comparison']
    if report.get('version',1)>=2:passed=passed and report.get('recommendation',{}).get('passed',False)
    review={'source_report':str(path),'source_report_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'commands':commands,'comparison_passed':report['comparison']['passed'],
        'source_unchanged_comparison':report['source_unchanged_comparison'],'passed':passed,
        'scope':'Named functional rules and SQL checks only; no answer quality judge or full 80-case acceptance',
        'original_report_preserved':True}
    target=path.with_name('rules_review.json')
    if target.exists():raise ValueError('Review already exists; do not overwrite evidence')
    write(target,review)
    print(json.dumps(review,ensure_ascii=False),flush=True)
    return 0 if passed else 1


def comparison(project, *, compare=True):
    from chat_runtime import stream_turn
    from evals.fixtures import CaseFixture
    from evals.observer import EvaluationObserver
    from postgres_config import connect_database
    from decimal import Decimal

    class SkillObserver(EvaluationObserver):
        def __init__(self):
            super().__init__(set())
            self.skill_seen = 0
            self.recommendation_seen = 0

        def on_chat_model_start(self, serialized, messages, **kwargs):
            # Record the presence, never copy prompt text into a diagnostic field.
            if any('商品对比操作说明' in str(message.content) for batch in messages for message in batch):
                self.skill_seen += 1
            if any('商品推荐与候选选择操作说明' in str(message.content) for batch in messages for message in batch):
                self.recommendation_seen += 1
            return super().on_chat_model_start(serialized, messages, **kwargs)

    query = ('比较' if compare else '推荐')+'两款适合通勤的自行车，预算5000以内，说明选择的商品描述依据。'
    observer = SkillObserver()
    chunks = []
    with CaseFixture('owner') as fixture:
        for event in stream_turn(project.app, query, fixture.thread_id, callbacks=[observer]):
            if event['type'] == 'text':
                chunks.append(event['text'])
        state = project.app.get_state({'configurable': {'thread_id': fixture.thread_id}}).values
        tasks = [task for task in state.get('tasks', []) if task['kind'] == 'product_search']
        ids = list(dict.fromkeys(pid for task in tasks for pid in task.get('displayed_product_ids', [])))
        with connect_database() as conn:
            reference = conn.execute('SELECT product_id,name,list_price,product_subcategory_id '
                'FROM product_catalog WHERE product_id=ANY(%s)', (ids,)).fetchall()
        by_id = {row['product_id']: row for row in reference}
        checks = {
            'search_task_present': bool(tasks),
            'comparison_loaded_only_when_requested': (observer.skill_seen > 0) if compare else observer.skill_seen == 0,
            'recommendation_present_in_real_model_input': observer.recommendation_seen > 0,
            'task_conditions_retained': bool(tasks) and all(task['parameters']['max_price'] == 5000
                and task['parameters']['product_category'] == 'bicycles' for task in tasks),
            'two_products_delivered': len(ids) == 2,
            'selected_ids_exist': bool(ids) and set(ids) == set(by_id),
            'selected_rows_match_independent_sql': bool(ids) and all(
                row['product_subcategory_id'] in (1, 2, 3) and 0 < Decimal(str(row['list_price'])) <= 5000
                for row in reference),
            'no_write_or_private_tools': all(tool['name'] in {'search_products', 'query_products',
                'get_product_info', 'calculate_price', 'check_promotion'} for tool in observer.tools),
            'model_calls_succeeded': bool(observer.models) and all(model['success'] for model in observer.models),
            'answer_present': bool(''.join(chunks)),
        }
        return {'input': query, 'answer': ''.join(chunks), 'tasks': tasks, 'tools': observer.tools,
            'models': observer.models, 'skill_input_count': observer.skill_seen,
            'recommendation_input_count': observer.recommendation_seen,
            'reference': reference, 'checks': checks, 'passed': all(checks.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--review',type=Path,help='Review recorded named rules; no model or business tool calls')
    args = parser.parse_args()
    if args.live and args.review:parser.error('Choose --live or --review')
    if args.review:return review_existing(args.review.resolve())
    if not args.live:
        print('No model calls. Pass --live for six existing scenarios, one two-turn budget case, one comparison and one recommendation.')
        return 0
    root = Path(__file__).resolve().parents[1]
    batch = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    directory = root / 'evals' / 'reports_composition' / batch
    directory.mkdir(parents=True, exist_ok=False)
    directory.chmod(0o700)
    report = {'version': 2, 'batch': batch, 'commands': [], 'limits': [
        'Authored development regressions, not unseen holdout',
        'No semantic suitability judge; SQL checks only prove the named facts and filters',
        'Not a load test or full 80-case acceptance', 'No rerun to select a passing answer'],
        'code_files': {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [*root.glob('*.py'), *root.glob('prompts/*.md'), *root.glob('config/*.json'),
                *root.glob('skills/*/SKILL.md')]}}
    print('REPORT_DIR', directory, flush=True)
    for label, command in [
        ('existing_six', ['run_current_evaluation.py', '--live', '--ids', 'G01,C01,O02,P01,N01,X01', '--no-judge']),
        ('budget_two_turn', ['-m', 'evals.constraint_runner', '--live', '--phase', 'e2e', '--ids', 'E03']),
    ]:
        with (directory / (label + '.log')).open('w') as log:
            result = subprocess.run([sys.executable, *command], cwd=root, stdout=log, stderr=subprocess.STDOUT)
        # A zero process status alone is not evidence that any cases ran.
        log_text=(directory / (label + '.log')).read_text(encoding='utf-8')
        report_created='REPORT_DIR ' in log_text
        report['commands'].append(recorded_command(directory,{'label': label, 'command': command,
            'returncode': result.returncode,'report_created':report_created}))
        write(directory / 'report.json', report)
        print(label, result.returncode, flush=True)
    import langgraph_supervisor_1 as project
    from chat_events import configure_event_log
    from evals.fixtures import source_fingerprint
    from deepseek_model import provider_settings
    from langsmith_tracing import flush_traces
    report['model'] = provider_settings()
    configure_event_log(directory / 'comparison-events.jsonl')
    report['source_before_comparison'] = source_fingerprint()
    try:
        report['comparison'] = comparison(project)
        write(directory / 'report.json',report)
        report['recommendation'] = comparison(project,compare=False)
    except Exception as exc:
        report['comparison'] = {'passed': False, 'error_type': type(exc).__name__}
    finally:
        report['source_after_comparison'] = source_fingerprint()
        report['source_unchanged_comparison'] = report['source_before_comparison'] == report['source_after_comparison']
        flush_traces()
        report['passed'] = (all(item['passed'] for item in report['commands'])
            and report['comparison']['passed'] and report.get('recommendation',{}).get('passed',False) and report['source_unchanged_comparison'])
        write(directory / 'report.json', report)
    print(json.dumps({'passed': report['passed'], 'comparison': report['comparison'].get('checks')}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
