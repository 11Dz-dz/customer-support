"""Real-model catalog matrix with hand-authored independent SQL references.

--phase acceptance is reserved until runtime code is frozen. No automatic retries.
"""
import argparse,hashlib,json
from pathlib import Path
from datetime import datetime,timezone
from uuid import uuid4

# SQL here deliberately does not call the application predicate compiler.
DEV=[
 ('Q01',['你们店里卖哪些类别的商品？'],'overview',None),
 ('Q02',['除了自行车还有什么交通工具'],'transport',None),
 ('Q03',['列出不是服装的商品，按标价从低到高取5条'],'catalog',"product_subcategory_id IS NOT NULL AND product_subcategory_id NOT BETWEEN 18 AND 25"),
 ('Q04',['列出红色或者银色的商品，标价600以内'],'catalog',"color IN ('Red','Silver') AND list_price<=600"),
 ('Q05',['列出预算1000以内的自行车，但不要黑色'],'catalog',"product_subcategory_id IN (1,2,3) AND color IS NOT NULL AND color<>'Black' AND list_price<=1000"),
 ('Q06',['列出自行车以外的商品，按标价最低取5条'],'catalog',"product_subcategory_id IS NOT NULL AND product_subcategory_id NOT IN (1,2,3)"),
 ('Q07',['列出100到500元之间的骑行服装'],'catalog',"product_subcategory_id BETWEEN 18 AND 25 AND list_price>=100 AND list_price<=500"),
 ('Q08',['列出不是银色、标价不超过800的自行车'],'catalog',"product_subcategory_id IN (1,2,3) AND color IS NOT NULL AND color<>'Silver' AND list_price<=800"),
 ('Q09',['列出满足任一条件的自行车：红色且价格600以内，或者银色且价格800以内。'],'catalog',"product_subcategory_id IN (1,2,3) AND ((color='Red' AND list_price<=600) OR (color='Silver' AND list_price<=800))"),
 ('Q10',['列出标价恰好539.99的自行车'],'catalog',"product_subcategory_id IN (1,2,3) AND list_price=539.99"),
 ('Q11',['列出紫色的自行车，只要紫色，不接受其他颜色'],'catalog',"product_subcategory_id IN (1,2,3) AND lower(color)='purple'"),
 ('Q12',['列出标价500以内但至少1000的自行车'],'clarify',None),
 ('Q13',['只看黑色自行车，但是不要黑色'],'clarify',None),
 ('Q14',['只看自行车配件，不要自行车整车，按价格列出5条'],'catalog',"(product_subcategory_id BETWEEN 4 AND 17 OR product_subcategory_id BETWEEN 26 AND 37)"),
 ('Q15',['列出价格严格低于500的骑行服装'],'catalog',"product_subcategory_id BETWEEN 18 AND 25 AND list_price<500"),
 ('Q16',['不是黑色的自行车共有多少条商品记录？'],'catalog',"product_subcategory_id IN (1,2,3) AND color IS NOT NULL AND color<>'Black'"),
 ('Q17',['列出600以内的自行车','不要黑色','改为红色或者银色'], 'catalog',"product_subcategory_id IN (1,2,3) AND color IN ('Red','Silver') AND list_price<=600"),
 ('Q18',['列出600以内的自行车','不要黑色','预算提高到1000','取消颜色条件'], 'catalog',"product_subcategory_id IN (1,2,3) AND list_price<=1000"),
 ('Q19',['列出600以内的自行车','另外新查骑行服装，价格不限'], 'catalog',"product_subcategory_id BETWEEN 18 AND 25"),
 ('Q20',['列出600以内的黑色自行车，再列出100以内的红色骑行服装'],'multi',None),
]
ACCEPTANCE=[
 ('A01',['不考虑黑色和银色，列出1200以内的自行车'],'catalog',"product_subcategory_id IN (1,2,3) AND color IS NOT NULL AND color NOT IN ('Black','Silver') AND list_price<=1200"),
 ('A02',['店里的交通工具除了单车还包括哪些种类？'],'transport',None),
 ('A03',['红色或蓝色的骑行服装有多少条，价格最多100？'],'catalog',"product_subcategory_id BETWEEN 18 AND 25 AND color IN ('Red','Blue') AND list_price<=100"),
 ('A04',['列出售价超过500但不高于800的自行车'],'catalog',"product_subcategory_id IN (1,2,3) AND list_price>500 AND list_price<=800"),
 ('A05',['列出银色自行车，价格800以内','不要银色了，换黑色，其他条件不变'],'catalog',"product_subcategory_id IN (1,2,3) AND color='Black' AND list_price<=800"),
 ('A06',['列出非黑色自行车，600以内','颜色不限，预算不变'],'catalog',"product_subcategory_id IN (1,2,3) AND list_price<=600"),
 ('A07',['列出尺寸40或42且不是黑色的自行车，1000以内'],'catalog',"product_subcategory_id IN (1,2,3) AND size IN ('40','42') AND color IS NOT NULL AND color<>'Black' AND list_price<=1000"),
 ('A08',['除了骑行服装和零部件，店里还有哪些商品类别？'],'overview_excluded',None),
]

TURN_REFERENCES={
 'Q17':["product_subcategory_id IN (1,2,3) AND list_price<=600","product_subcategory_id IN (1,2,3) AND list_price<=600 AND color IS NOT NULL AND color<>'Black'","product_subcategory_id IN (1,2,3) AND color IN ('Red','Silver') AND list_price<=600"],
 'Q18':["product_subcategory_id IN (1,2,3) AND list_price<=600","product_subcategory_id IN (1,2,3) AND list_price<=600 AND color IS NOT NULL AND color<>'Black'","product_subcategory_id IN (1,2,3) AND list_price<=1000 AND color IS NOT NULL AND color<>'Black'","product_subcategory_id IN (1,2,3) AND list_price<=1000"],
 'Q19':["product_subcategory_id IN (1,2,3) AND list_price<=600","product_subcategory_id BETWEEN 18 AND 25"],
 'A05':["product_subcategory_id IN (1,2,3) AND color='Silver' AND list_price<=800","product_subcategory_id IN (1,2,3) AND color='Black' AND list_price<=800"],
 'A06':["product_subcategory_id IN (1,2,3) AND color IS NOT NULL AND color<>'Black' AND list_price<=600","product_subcategory_id IN (1,2,3) AND list_price<=600"],
}

def review_intermediate_turns(report):
    """Offline SQL review of recorded answers; never executes the agent or tools."""
    from postgres_config import connect_database
    reviewed=[]
    with connect_database() as c:
        for row in report['results']:
            refs=TURN_REFERENCES.get(row['id'])
            if not refs:continue
            if len(refs)!=len(row['turns']):
                reviewed.append({'id':row['id'],'passed':False,'reason':'Missing recorded turns'});continue
            for index,(turn,where) in enumerate(zip(row['turns'],refs)):
                reference=c.execute('SELECT product_id FROM product_catalog WHERE list_price>0 AND ('+where+')').fetchall();ids={r['product_id'] for r in reference}
                catalogs=[t['result'] for t in turn['tools'] if t['name']=='query_products' and t.get('success')]
                ok=len(catalogs)==1 and catalogs[0].get('matched_count')==len(ids) and all(p['product_id'] in ids for p in catalogs[0].get('products',[]))
                reviewed.append({'id':row['id'],'turn':index+1,'passed':ok,'independent_sql':where,'reference_count':len(ids)})
    return {'passed':all(r['passed'] for r in reviewed),'turns':reviewed,'agent_calls':0}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--phase',choices=['dev','acceptance'],default='dev');parser.add_argument('--ids');parser.add_argument('--review',type=Path);args=parser.parse_args()
    if args.review:
        from evals.fixtures import source_fingerprint
        report=json.loads(args.review.read_text(encoding='utf-8'));assert report['source_after']==source_fingerprint() and report['source_unchanged']
        review=review_intermediate_turns(report);review['passed']=review['passed'] and report['passed'];review['source_report_sha256']=hashlib.sha256(args.review.read_bytes()).hexdigest();review['reviewer_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        target=args.review.parent/'all_turns_review.json'
        with target.open('x',encoding='utf-8') as f:json.dump(review,f,ensure_ascii=False,indent=2)
        print(json.dumps(review,ensure_ascii=False));return 0 if review['passed'] else 1
    if not args.live:print('No model calls; explicit --live required.');return 0
    import langgraph_supervisor_1 as project
    from chat_runtime import stream_turn
    from chat_events import configure_event_log
    from evals.fixtures import CaseFixture,source_fingerprint
    from evals.observer import EvaluationObserver
    from postgres_config import connect_database
    batch=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
    root=Path(__file__).resolve().parents[1];directory=root/'evals/reports_catalog_query'/batch;directory.mkdir(parents=True)
    configure_event_log(directory/'events.jsonl')
    cases=DEV if args.phase=='dev' else ACCEPTANCE
    if args.ids:cases=[c for c in cases if c[0] in args.ids.split(',')]
    report={'batch':batch,'phase':args.phase,'source_before':source_fingerprint(),'results':[],
        'code_files':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [*root.glob('*.py'),*root.glob('prompts/*.md'),*root.glob('config/*.json')]},
        'limits':['Authored functional and SQL matrix, not independent human acceptance','No semantic quality judge or load test','Raw failures retained; no rerun without a documented change']}
    def save():(directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print('REPORT_DIR',directory,flush=True);save()
    try:
        for cid,texts,mode,where in cases:
            turns=[]
            with CaseFixture('owner') as fixture:
                for text in texts:
                    observer=EvaluationObserver(set());chunks=[];plans=[]
                    from unittest.mock import patch
                    original_validator=project.validate_planner_counts
                    def observe_plan(*a,**kw):
                        plans.append(a[0].model_dump(mode='json'));return original_validator(*a,**kw)
                    plan_spy=patch.object(project,'validate_planner_counts',side_effect=observe_plan);plan_spy.start()
                    for event in stream_turn(project.app,text,fixture.thread_id,callbacks=[observer]):
                        if event['type']=='text':chunks.append(event['text'])
                    plan_spy.stop()
                    if any(m.get('provider_status_code') in (401,402,403,429) for m in observer.models):raise RuntimeError('Provider unavailable; evaluation stopped')
                    state=project.app.get_state({'configurable':{'thread_id':fixture.thread_id}}).values
                    turns.append({'input':text,'answer':''.join(chunks),'tools':observer.tools,'models':observer.models,
                        'planner_proposals':plans,'active_ids':state.get('turn_task_ids',[]),'tasks':state.get('tasks',[])})
                final=turns[-1];tools=final['tools'];catalogs=[t['result'] for t in tools if t['name']=='query_products' and t.get('success')]
                checks={'answer_present':bool(final['answer']),'no_unexpected_tools':all(t['name']=='query_products' for t in tools),
                    'models_succeeded':all(m.get('success') for turn in turns for m in turn['models'])}
                reference=[]
                with connect_database() as c:
                    if mode=='catalog':
                        reference=c.execute('SELECT product_id FROM product_catalog WHERE list_price>0 AND ('+where+') ORDER BY product_id').fetchall();ids={p['product_id'] for p in reference}
                        checks['one_exact_catalog_query']=len(catalogs)==1 and len(tools)==1
                        checks['full_count_matches_independent_sql']=len(catalogs)==1 and catalogs[0].get('matched_count')==len(ids)
                        shown=catalogs[0].get('products',[]) if catalogs else []
                        checks['shown_ids_satisfy_sql']=all(p['product_id'] in ids for p in shown)
                        checks['not_recommendation_path']=all(m['role']=='router' for m in final['models']) and '未能根据描述' not in final['answer']
                    elif mode=='clarify':checks['clarifies_without_query']=not tools and ('确认' in final['answer'] or '矛盾' in final['answer'] or '?' in final['answer'] or '？' in final['answer'])
                    elif mode=='multi':
                        checks['two_queries']=len(catalogs)==2 and len(tools)==2
                        predicates=["product_subcategory_id IN (1,2,3) AND color='Black' AND list_price<=600","product_subcategory_id BETWEEN 18 AND 25 AND color='Red' AND list_price<=100"]
                        expected=[c.execute('SELECT count(*) AS n FROM product_catalog WHERE list_price>0 AND '+p).fetchone()['n'] for p in predicates]
                        checks['separate_scope_counts']=sorted(r.get('matched_count') for r in catalogs)==sorted(expected)
                    else:
                        checks['one_overview']=len(catalogs)==1 and len(tools)==1 and catalogs[0].get('operation')=='overview'
                        expected=set() if mode=='transport' else {'bicycles','accessories'} if mode=='overview_excluded' else {'bicycles','components','clothing','accessories'}
                        checks['exact_categories']=len(catalogs)==1 and {r['category'] for r in catalogs[0].get('categories',[])}==expected
                        unknown=c.execute('SELECT count(*) AS n FROM product_catalog WHERE product_subcategory_id IS NULL').fetchone()['n']
                        checks['unknown_not_claimed_absent']=len(catalogs)==1 and catalogs[0].get('unclassified_count')==unknown and (not unknown or '未分类' in final['answer'])
                        checks['no_recommendation_fallback']='未能根据描述' not in final['answer']
                row={'id':cid,'mode':mode,'turns':turns,'sql_reference_ids':[r['product_id'] for r in reference],'checks':checks,'passed':all(checks.values())};report['results'].append(row);save();print(cid,row['passed'],[k for k,v in checks.items() if not v],flush=True)
    finally:
        report['source_after']=source_fingerprint();report['source_unchanged']=report['source_after']==report['source_before']
        report['passed']=len(report['results'])==len(cases) and all(r['passed'] for r in report['results']) and report['source_unchanged'];save()
    print(json.dumps({'passed':report['passed'],'scenarios':len(report['results']),'turns':sum(len(r['turns']) for r in report['results'])}),flush=True)
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
