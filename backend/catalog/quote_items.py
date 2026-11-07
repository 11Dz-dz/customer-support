"""Bind quote line quantities to raw user input or the explicit prior task.

This is not a cart or order writer. No price is accepted from the planner.
"""
import re
from decimal import Decimal
from backend.tasks.constraint_updates import NUMBER,number,recognized,ConstraintConflict
from backend.tasks.task_state import QuoteLine,missing_fields


def quantity_value(token):
    value=Decimal(str(number(token.lstrip('-'))))*(-1 if token.startswith('-') else 1)
    if value!=value.to_integral_value() or not 1<=value<=10000:
        raise ConstraintConflict('quantity','每件商品的购买数量必须是1到10000的整数')
    return int(value)


def explicit_lines(inputs):
    found={};sources={}
    for index,text in enumerate(inputs):
        matches=list(re.finditer(r'(?:商品\s*|\bproduct\s+|(?<=\d)[、和及加]\s*)(\d+)',text,re.I))
        all_quantities={quantity_value(x[1]) for x in re.finditer(rf'(-?{NUMBER})\s*(?:件|辆|台)',text)}
        shared=next(iter(all_quantities)) if len(all_quantities)==1 and re.search(r'每款|每种|各',text) else None
        for n,m in enumerate(matches):
            end=matches[n+1].start() if n+1<len(matches) else len(text)
            segment=text[m.end():end]
            quantities={quantity_value(x[1]) for x in re.finditer(rf'(-?{NUMBER})\s*(?:件|辆|台)',segment)}
            if len(quantities)>1:raise ConstraintConflict('quantity','同一商品出现多个购买数量，请明确各商品数量')
            if not quantities and shared is None:continue
            qty=shared if shared is not None else next(iter(quantities));pid=int(m[1])
            if not 1<=qty<=10000:raise ConstraintConflict('quantity','每件商品的购买数量必须是1到10000的整数')
            if pid in found and sources[pid]['source_index']==index:
                raise ConstraintConflict('quantity','同一商品重复出现，请明确最终购买数量')
            found[pid]=qty;sources[pid]={'source_index':index,'source_text':text if shared is not None else text[m.start():end],
                'verified_by':'explicit_quote_line','operation':'set'}
    return [QuoteLine(product_id=pid,quantity=qty) for pid,qty in found.items()],sources


def scoped_lines(inputs,item,*,single_scope=True):
    raw,sources=explicit_lines(inputs)
    if not single_scope:
        wanted=set(item.parameters.product_ids)|{line.product_id for line in item.parameters.quote_items}
        if not wanted:raise ConstraintConflict('quantity','多项报价需要分别明确每项商品归属')
        raw=[line for line in raw if line.product_id in wanted]
        sources={pid:s for pid,s in sources.items() if pid in wanted}
    return raw,sources


def bind_quote(task,item,previous,inputs,*,single_scope=True):
    raw,sources=scoped_lines(inputs,item,single_scope=single_scope)
    proposed=item.parameters.quote_items
    same=bool(previous and previous.kind=='quote' and previous.task_id==task.task_id)
    old=previous.parameters.quote_items if same else []
    legacy=bool(same and not old and previous.parameters.quantity and previous.parameters.product_ids)
    if legacy:old=[QuoteLine(product_id=pid,quantity=previous.parameters.quantity) for pid in previous.parameters.product_ids]
    raw_facts={}
    for text in inputs:raw_facts.update(recognized(text)[0])
    common=raw_facts.get('quantity')
    verified=set(previous.displayed_product_ids) if previous else set()
    if same:verified.update(previous.parameters.product_ids)
    shared=any(re.search(r'各|每款|每种',text) for text in inputs)
    from backend.catalog.product_references import explicit_position
    position=explicit_position('\n'.join(inputs))
    ordered=previous.displayed_product_ids if previous else []
    if position is not None and 1<=position<=len(ordered):verified={ordered[position-1]}
    explicit={int(m[1]) for text in inputs for m in re.finditer(r'(?:商品\s*|\bproduct\s+|(?<=\d)[、和及加]\s*)(\d+)',text,re.I)}
    declared=list(dict.fromkeys(task.parameters.product_ids+[line.product_id for line in proposed]+[line.product_id for line in raw]))
    if not declared and single_scope:declared=sorted(explicit)
    if same:declared=list(dict.fromkeys(previous.parameters.product_ids+declared))
    if not set(declared)<=explicit|verified:
        raise ConstraintConflict('quantity','报价商品缺少用户明确编号或已核实的展示依据',repairable=True)
    known={line.product_id:line.quantity for line in old}
    known.update({line.product_id:line.quantity for line in raw})
    pending=[pid for pid in declared if pid not in known]
    if not raw and common is not None:
        common=quantity_value(str(common))
        changed=[]
        if pending:
            if len(pending)>1 and not shared:
                # The first request may explicitly express a uniform quantity;
                # a bare reply to multiple missing line quantities is ambiguous.
                if same:raise ConstraintConflict('quantity','多件商品缺少数量，请分别说明或明确每款相同数量')
            for pid in pending:known[pid]=common;changed.append(pid)
        elif same:
            known={pid:common for pid in declared}
            changed=list(declared)
        for index,text in reversed(list(enumerate(inputs))):
            if recognized(text)[0].get('quantity')==common:
                for pid in changed:sources[pid]={'source_index':index,'source_text':text,'verified_by':'pending_quote_quantity','operation':'set'}
                break
    for line in proposed:
        if known.get(line.product_id)!=line.quantity:
            raise ConstraintConflict('quantity','逐商品数量找不到对应原文或原报价明细',repairable=True)
    task.parameters.product_ids=declared
    task.parameters.quote_items=[QuoteLine(product_id=pid,quantity=known[pid]) for pid in declared if pid in known]
    values={line.quantity for line in task.parameters.quote_items}
    task.parameters.quantity=(next(iter(values)) if len(values)==1 and len(known)==len(declared) else
        common if not declared and common is not None else None)
    if task.parameters.quote_items:
        prior_sources=previous.parameter_sources.get('quote_items',{}).get('lines',{}) if same else {}
        merged={str(pid):value for pid,value in prior_sources.items()}
        merged.update({str(pid):{**value,'revision':task.revision} for pid,value in sources.items()})
        task.parameter_sources['quote_items']={'verified_by':'quote_line_binding','lines':merged,
            'inherited_task_id':previous.task_id if previous else None,'legacy_scalar_adapted':legacy,'revision':task.revision}
    task.awaiting_fields=missing_fields(task)
    task.status='awaiting_input' if task.awaiting_fields else 'ready'
    return task
