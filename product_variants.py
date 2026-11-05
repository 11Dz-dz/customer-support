"""Bind a factual SKU-variant lookup to a verified displayed product."""
import re
from copy import deepcopy
from uuid import uuid4
from constraint_updates import recognized,ConstraintConflict
from task_state import TaskParameters
from task_result import collect_evidence


def bind_variant_task(task,item,previous,state,inputs):
    text='\n'.join(inputs)
    axes={axis for axis,pattern in [('color',r'颜色|花色|\bcolou?rs?\b'),('size',r'尺寸|尺码|\bsizes?\b')] if re.search(pattern,text,re.I)}
    if len(axes)>1:
        if item.parameters.variant_axis not in axes:raise ConstraintConflict('size','请分别说明要查询颜色还是尺寸')
        axis=item.parameters.variant_axis
    else:axis=next(iter(axes),None)
    if axis is None:
        raw,_=recognized(text)
        if raw.get('size') is not None:axis='size'
        elif raw.get('color') is not None:axis='color'
    if axis is None and previous and previous.kind=='product_variants':axis=previous.parameters.variant_axis
    explicit=[int(n) for n in re.findall(r'(?:商品\s*|\bproduct\s+)(\d+)',text,re.I)]
    displayed=list(previous.displayed_product_ids) if previous else []
    if not displayed and previous and previous.kind=='product_variants' and previous.parameters.variant_anchor_id:
        displayed=[previous.parameters.variant_anchor_id]
    evidence=collect_evidence(state.get('messages',[]))
    # Older failed recommendations may have cleared structured selection. Recover
    # only a server-rendered public SKU heading, backed by successful catalog evidence.
    if not displayed and previous:
        allowed=set(previous.evidence_call_ids)
        known=set()
        for e in evidence:
            if e.call_id not in allowed or e.tool not in {'search_products','query_products','get_product_info','query_product_variants'} or e.data.get('success') is not True:continue
            rows=e.data.get('products',[])+([e.data['product']] if isinstance(e.data.get('product'),dict) else [])
            known.update(p.get('product_id') for p in rows if type(p.get('product_id')) is int)
        for message in reversed(state.get('messages',[])):
            if getattr(message,'type',None)!='ai' or getattr(message,'tool_calls',[]) or getattr(message,'name',None)!='sales':continue
            ids=[int(n) for n in re.findall(r'(?:^|\n)商品(\d+)[:：]',str(message.content))]
            if ids:
                displayed=[pid for pid in ids if pid in known]
                break
    if explicit:
        if len(set(explicit))!=1:raise ConstraintConflict('product_category','请一次明确查询一件商品的同款规格')
        pid=explicit[0]
    else:
        from product_references import explicit_position
        position=explicit_position(text)
        if position is not None:
            if not 1<=position<=len(displayed):raise ConstraintConflict('product_category','商品位置超出已展示清单')
            pid=displayed[position-1]
        elif len(displayed)==1:pid=displayed[0]
        elif displayed:raise ConstraintConflict('product_category','此前展示多件商品，请说明查询哪件的颜色或尺寸')
        else:pid=None
    values=previous.parameters.model_dump() if previous else TaskParameters().model_dump()
    facts={};clears=set()
    for index,source in enumerate(inputs):
        extracted,removed=recognized(source);facts.update(extracted);clears.update(removed)
        for key in ('min_price','max_price'):
            if key in extracted:values[key]=extracted[key]
            if key in removed:values[key]=None
    # Only a current, explicit requested facet constrains the changing dimension.
    if axis:values[axis]=facts.get(axis)
    other='size' if axis=='color' else 'color'
    values[other]=facts.get(other)  # Otherwise the catalog reference SKU supplies it.
    from relative_price import relative_request
    relation=relative_request(inputs)
    strict=bool(relation or set(facts)&{'min_price','max_price'} or re.search(
        r'(?:预算|价格|价位).{0,12}(?:不变|别改|不改|不动|维持|照旧|以内|以下|范围|保持)|(?:只看|只要|保持|保留|仍按|不超).{0,12}(?:预算|价格|价位)',text))
    values.update(product_ids=[pid] if pid is not None else [],product_position=None,variant_axis=axis,variant_anchor_id=pid,
                  variant_strict=strict,variant_relation=relation[0] if relation else None,limit=20,clear_fields=[])
    task.parameters=TaskParameters.model_validate(values)
    task.task_id=uuid4().hex;task.revision=1  # Information view; never edit the source recommendation.
    task.parameter_sources=deepcopy(previous.parameter_sources) if previous else {}
    for key in ('min_price','max_price'):
        if key in facts or key in clears:
            task.parameter_sources[key]={'verified_by':'business_normalizer','operation':'clear' if key in clears else 'set',
                'source_text':text,'revision':task.revision}
    if values['min_price'] is not None and values['max_price'] is not None and values['min_price']>values['max_price']:
        raise ConstraintConflict('max_price','最低价格高于最高价格')
    task.parameter_sources['product_ids']={'verified_by':'explicit_user_sku' if explicit else 'displayed_catalog_reference',
        'source_text':text,'reference_product_id':pid}
    from task_state import missing_fields
    task.awaiting_fields=missing_fields(task);task.status='awaiting_input' if task.awaiting_fields else 'ready'
    return task


def render_variants(value):
    from answer_contract import money,cell
    from decimal import Decimal
    axis=value['variant_axis'];label='颜色' if axis=='color' else '尺寸';anchor=value['anchor']
    if value.get('variant_status')=='model_unknown':
        return '目录缺少该商品的型号关联，暂时无法核实同款其他'+label+'。'
    fixed=value.get('fixed_spec',{});fixed_label={'size':'尺寸','color':'颜色'}.get(fixed.get('field'),'规格')
    lines=[f"按商品{anchor['product_id']}的同一型号查询{label}。"]
    if fixed.get('value') is not None:lines.append(f"保留{fixed_label}：{cell(fixed['value'])}。")
    filters=value.get('price_filters',{});parts=[]
    if filters.get('min_price') is not None:parts.append('不低于'+money(filters['min_price']))
    if filters.get('max_price') is not None:parts.append('不超过'+money(filters['max_price']))
    if value.get('price_relation'):
        words={'lt':'低于','le':'不高于','gt':'高于','ge':'不低于'}
        parts.append(words[value['price_relation']]+'原商品目录标价'+money(anchor['list_price']))
    products=value['products']
    if not products:
        excluded=value.get('excluded_summary',{})
        if excluded.get('count'):
            lines.append('同型号有其他'+label+'（'+ '、'.join(cell(x) for x in excluded['values'])+'），但不符合当前价格条件，未列为符合条件的款式。')
            if excluded.get('min_price') is not None:lines.append('这些款式的最低历史目录标价：'+money(excluded['min_price'])+'。')
        else:lines.append('当前样例目录在上述规格下没有记录其他'+label+'，不能据此判断实时库存。')
    for p in products:
        lines.append(f"商品{p['product_id']}：{cell(p['name'])}。颜色：{cell(p.get('color') or '未提供')}；尺寸：{cell(p.get('size') or '未提供')}。")
        lines.append('历史目录标价：'+money(p['list_price'])+'。' if p.get('price_known') else '目录标价无有效报价，不能视为免费。')
        if p.get('price_difference') is not None:
            diff=Decimal(str(p['price_difference']));lines.append('与原商品标价相同。' if not diff else '比原商品'+('贵' if diff>0 else '便宜')+money(abs(diff))+'。')
        if parts and p.get('price_known'):
            lines.append(('符合' if p['within_price'] else '不符合')+'原价格条件（'+'，'.join(parts)+'）。')
    if value.get('strict'):lines.append('本次按价格条件严格筛选。')
    elif parts:lines.append('本次展示同款规格资料，并标注原价格条件；原推荐条件继续保留。')
    if value.get('truncated'):lines.append(f"仅展示前{value['returned_count']}款，共{value['matched_count']}款。")
    lines.append('以上为AdventureWorks历史样例目录信息，非实时售价；币种、库存及在售状态未知。')
    return '\n'.join(lines)
