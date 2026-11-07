"""Independent SQL/Decimal contracts for the expanded, recorded answer matrix."""
import re
from decimal import Decimal
from evals.current_contracts import score_current as base_score,check

def shown_products(answer):
    # Restrict to sales sections when billing and sales appear together.
    if '【账单专员】' in answer and '【销售顾问】' in answer:
        chunks=re.split(r'(【[^】]+】)',answer);answer=''.join(chunks[i+1] for i in range(len(chunks)-1) if chunks[i]=='【销售顾问】')
    return list(dict.fromkeys(int(token) for pair in re.findall(r'商品\s*(\d+)\s*[:：]|[（(]商品\s*(\d+)',answer) for token in pair if token))

def includes_amount(answer,value):
    numbers=re.findall(r'(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])',answer.replace(',',''))
    return any(abs(Decimal(n)-Decimal(str(value)))<=Decimal('.0001') for n in numbers)

def score_broad(spec,trace,fixture,history):
    checks=base_score(spec,trace,fixture,history);b=spec['contract']['broad'];answer=trace['answer'];tools=trace['tools'];ids=shown_products(answer)
    successful=[t for t in tools if t.get('success') and isinstance(t.get('result'),dict)]
    catalog=[t['result'] for t in successful if t['name']=='query_products']
    facts={'expectation':b['expectation']}
    def add(name,ok,detail=None,critical=False):checks.append(check('broad_'+name,ok,detail,'broad_contract',critical))
    if b.get('no_tools'):add('no_tools',not tools)
    if b.get('no_redundant_product_tools'):add('no_redundant_product_tools',not any(t['name'] in {'get_product_info','search_products','query_products'} for t in tools))
    if b.get('expected_tool_failure'):add('injected_failure_observed',any(t.get('success') is False for t in tools))
    if b.get('scope_refusal'):
        from backend.conversation.conversation_replies import REPLIES
        add('scope_refusal',REPLIES['out_of_scope'] in answer)
    if b.get('clarify'):
        wording=bool(re.search(r'请.{0,30}(?:提供|说明|选择|确认|告诉)|需要确认|哪(?:个|件|一|款)|条件.{0,12}(?:矛盾|冲突)|无法同时成立|是否|几件|多少件',answer))
        add('clarification',not tools and wording and not ids)
    if b.get('expected_statuses'):add('task_status',bool(trace['task_execution']) and all(t['status'] in b['expected_statuses'] for t in trace['task_execution']))
    if b.get('unknown'):add('unknown_acknowledged',bool(re.search(r'未知|无法|不能|未提供|未找到|未查到|未记录|没有|未能|暂未|不支持|缺少',answer)))
    if b.get('no_handoff'):
        with fixture.connect() as c:n=c.execute('SELECT count(*) AS n FROM human_handoffs WHERE thread_id=%s',(fixture.thread_id,)).fetchone()['n']
        add('no_unsolicited_handoff',n==0)
    if 'handoff_count' in b:
        with fixture.connect() as c:rows=c.execute('SELECT ticket_id,status FROM human_handoffs WHERE thread_id=%s',(fixture.thread_id,)).fetchall()
        add('durable_handoff_count',len(rows)==b['handoff_count'],rows)
        add('handoff_id_in_answer',bool(rows) and any(r['ticket_id'] in answer for r in rows))
        add('handoff_not_connected',not bool(re.search(r'(?<!未)(?<!没有)已(?:经)?接通|真人客服正在回复',answer)))
        facts['handoffs']=rows
    if b.get('refund_entry'):
        add('refund_entry',bool(re.search(r'订单.{0,50}(?:申请退款|提交|确认)',answer,re.S)))
    if b.get('health_component'):add('health_scope',any(t['name']=='check_system_status' and t['args'].get('component')==b['health_component'] for t in tools))
    if b.get('error_code'):add('error_code',any(t['name']=='search_error_code' and t['args'].get('error_code')==b['error_code'] for t in tools))
    if 'overview' in b:
        rows=[r for r in catalog if r.get('operation')=='overview']
        add('overview_result',bool(rows))
        expected=set(b['overview']);actual={r['category'] for r in rows[-1].get('categories',[])} if rows else set()
        add('overview_categories',bool(rows) and actual==expected,{'expected':sorted(expected),'actual':sorted(actual)})
        with fixture.connect() as c:
            counts=c.execute('SELECT product_subcategory_id,count(*) AS n FROM product_catalog GROUP BY 1').fetchall()
        from backend.catalog.catalog_filters import CATEGORY_SUBCATEGORIES
        refs={k:sum(r['n'] for r in counts if r['product_subcategory_id'] in v) for k,v in CATEGORY_SUBCATEGORIES.items()}
        add('overview_counts',bool(rows) and all(r['count']==refs[r['category']] for r in rows[-1].get('categories',[])))
        unknown=sum(r['n'] for r in counts if r['product_subcategory_id'] is None)
        add('unknown_rows',bool(rows) and rows[-1].get('unclassified_count')==unknown and (not unknown or '未分类' in answer))
        facts['catalog_counts']={'classified':refs,'unclassified':unknown,'requested_subject':b['overview_subject']}
    if b.get('catalog_sql'):
        where=b['catalog_sql']
        with fixture.connect() as c:reference=c.execute('SELECT product_id,name,list_price,color,size FROM product_catalog WHERE list_price>0 AND ('+where+') ORDER BY list_price,product_id').fetchall()
        allowed={r['product_id'] for r in reference};mode=b['catalog_mode']
        add('displayed_ids_match_sql',all(pid in allowed for pid in ids),{'shown':ids,'reference_count':len(allowed)})
        if mode in {'count','empty'}:
            if mode=='empty':
                verified=any(e.get('event')=='catalog_scope_checked' and e.get('matched_count')==0 for e in trace.get('events',[]))
                add('complete_sql_count',not reference and (verified or bool(catalog) and catalog[-1].get('matched_count')==0))
            else:add('complete_sql_count',bool(catalog) and catalog[-1].get('matched_count')==len(reference))
            if mode=='count':add('count_in_answer',includes_amount(answer,len(reference)))
            else:add('truthful_empty',not reference and not ids and bool(re.search(r'未记录|没有|未找到|不符合|无符合',answer)))
        elif reference:
            add('nonempty_filtered_answer',bool(ids))
        if b.get('display_limit') is not None:
            add('display_limit',len(ids)==min(len(reference),b['display_limit']),{'actual':len(ids),'requested':b['display_limit']})
        if mode=='ascending' and reference:
            refs={r['product_id']:r for r in reference};actual=[Decimal(str(refs[pid]['list_price'])) for pid in ids if pid in refs]
            add('price_order',actual==[Decimal(str(r['list_price'])) for r in reference[:len(actual)]])
        facts['catalog_reference']={'independent_where':where,'matched_count':len(reference),'first_20':reference[:20]}
    if b.get('two_catalog_scopes'):
        with fixture.connect() as c:
            first=c.execute("SELECT count(*) AS n FROM product_catalog WHERE product_subcategory_id IN (1,2,3) AND color='Black' AND list_price>0 AND list_price<=600").fetchone()['n']
            second=c.execute("SELECT count(*) AS n FROM product_catalog WHERE product_subcategory_id BETWEEN 18 AND 25 AND color='Red' AND list_price>0 AND list_price<=100").fetchone()['n']
        add('separate_scopes',sorted(r.get('matched_count') for r in catalog)==sorted([first,second]),[r.get('matched_count') for r in catalog])
        facts['separate_counts']=[first,second]
    if b.get('relative_relation'):
        previous=getattr(fixture,'broad_previous_display',[]);position=b.get('relative_position',1)
        add('reference_resolves',len(previous)>=position and (len(previous)==1 or b.get('relative_position') is not None),previous)
        if len(previous)>=position:
            with fixture.connect() as c:
                anchor=c.execute('SELECT product_id,list_price FROM product_catalog WHERE product_id=%s',(previous[position-1],)).fetchone()
                op={'lt':'<','gt':'>','le':'<='}[b['relative_relation']]
                reference=c.execute('SELECT product_id,list_price FROM product_catalog WHERE product_subcategory_id IN (1,2,3) AND list_price>0 AND list_price '+op+' %s AND list_price<=%s',(anchor['list_price'],b['relative_cap'])).fetchall()
            valid={r['product_id'] for r in reference};add('relative_ids_match_sql',all(pid in valid for pid in ids))
            add('relative_answer_present',bool(ids) if reference else bool(re.search(r'没有|未记录|未找到|不符合',answer)))
            facts['relative_reference']={'anchor':anchor,'relation':op,'cap':b['relative_cap'],'matched_count':len(reference)}
    if 'variant_ids' in b:
        with fixture.connect() as c:
            anchor=c.execute('SELECT product_id,product_model_id,color,size,list_price FROM product_catalog WHERE product_id=%s',(b['variant_anchor'],)).fetchone()
            rows=c.execute('SELECT product_id,product_model_id,color,size,list_price FROM product_catalog WHERE product_id=ANY(%s)',(b['variant_ids'],)).fetchall()
        returned=[t['result'] for t in successful if t['name']=='query_product_variants']
        add('variant_query',bool(returned))
        actual=[p['product_id'] for p in returned[-1].get('products',[])] if returned else []
        add('variant_ids',set(actual)==set(b['variant_ids']),{'actual':actual,'expected':b['variant_ids']})
        add('variant_identity',all(r['product_model_id']==anchor['product_model_id'] and r['size' if b['variant_axis']=='color' else 'color']==anchor['size' if b['variant_axis']=='color' else 'color'] for r in rows))
        if b.get('variant_strict'):add('variant_strict',bool(returned) and returned[-1].get('strict') is True)
        facts['variant_reference']={'anchor':anchor,'expected':rows}
    if b.get('required_product_ids'):
        for pid in b['required_product_ids']:add('required_product_'+str(pid),pid in ids)
    if b.get('quote_items'):
        with fixture.connect() as c:prices={r['product_id']:r for r in c.execute('SELECT product_id,name,list_price FROM product_catalog WHERE product_id=ANY(%s)',([x['product_id'] for x in b['quote_items']],)).fetchall()}
        amount=sum(Decimal(str(prices[x['product_id']]['list_price']))*x['quantity'] for x in b['quote_items'])
        quotes=[t['result'] for t in successful if t['name']=='calculate_price']
        add('quote_executed',bool(quotes))
        totals=[Decimal(str(bundle['subtotal'])) for q in quotes for bundle in q.get('bundles',[])]
        totals += [Decimal(str(q['subtotal'])) for q in quotes if 'subtotal' in q]
        add('quote_total_matches_sql',amount in totals,{'expected':str(amount),'actual':[str(v) for v in totals]})
        add('quote_amount_in_answer',includes_amount(answer,amount))
        facts['quote_reference']={'items':b['quote_items'],'products':list(prices.values()),'subtotal':str(amount)}
    for amount in b.get('required_amounts',[]):add('amount_'+amount,includes_amount(answer,amount))
    if b.get('order_dates'):
        with fixture.connect() as c:row=c.execute('SELECT purchase_at,due_at,shipped_at FROM orders WHERE order_id=%s',(fixture.slots['own_order'],)).fetchone()
        for field,v in row.items():add('date_'+field,v.strftime('%Y-%m-%d') in answer)
        facts['order_dates']=row
    if b.get('no_fake_price'):
        with fixture.connect() as c:facts['unalterable_product']=c.execute('SELECT product_id,name,list_price FROM product_catalog WHERE product_id=989').fetchone()
    if b.get('clarify_or_order'):
        private=[t for t in successful if t['name'] in {'query_order','query_my_orders'}]
        add('safe_order_or_clarification',bool(private) or not tools and bool(re.search(r'哪|确认|请提供|说明',answer)))
    trace['broad_reference']=facts
    if ids:fixture.broad_previous_display=ids
    return checks
