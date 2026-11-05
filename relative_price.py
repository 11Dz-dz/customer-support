"""Ground comparative catalog prices in displayed, successful tool evidence.

The current catalog stores numeric(19,4); strict comparisons use that quantum,
not a currency cent. No model-generated price or assistant prose is trusted.
"""
from decimal import Decimal,InvalidOperation
import re

from constraint_updates import ConstraintConflict,recognized

def relative_request(inputs):
    patterns={
        'le':r'不比[^，。；;\n]{0,35}贵|不高于(?:这个|这款|刚才|上一款)[^，。；;\n]{0,15}(?:价|标价)',
        'ge':r'不比[^，。；;\n]{0,35}便宜|不低于(?:这个|这款|刚才|上一款)[^，。；;\n]{0,15}(?:价|标价)',
        'lt':r'更便宜|便宜(?:一点|点|一些)|比[^，。；;\n]{0,35}(?:便宜|价格低)|\bcheaper\b|\bless expensive\b',
        'gt':r'更贵|贵(?:一点|点|一些)|比[^，。；;\n]{0,35}(?:贵|价格高)|\bmore expensive\b',
    }
    found=None
    for index,text in enumerate(inputs):
        inclusive={key for key in ('le','ge') if re.search(patterns[key],text,re.I)}
        directions=inclusive or {key for key in ('lt','gt') if re.search(patterns[key],text,re.I)
            and not re.search(r'(?:不要|不用|不想要)\s*(?:'+patterns[key]+')',text,re.I)}
        if len(directions)>1:raise ConstraintConflict('max_price','相对价格方向不明确，请说明要更便宜还是更贵')
        if directions:
            if re.search(r'(?:便宜|更贵|贵|价格低|价格高)\s*(?!一点|一些)[0-9一二两三四五六七八九十百]+',text):
                raise ConstraintConflict('max_price','请提供明确的目标预算；暂不将差额或比例比较换算成价格条件')
            found=(next(iter(directions)),index,text)
    return found


def reference_bound(previous,item,messages,request):
    from task_scheduler import task_evidence
    direction,index,text=request
    ids=previous.displayed_product_ids
    if not ids:raise ConstraintConflict('max_price','没有可核对的已展示商品，请先说明要比较的商品')
    explicit=[int(n) for n in re.findall(r'商品\s*(\d+)',text)]
    from product_references import explicit_position
    position=explicit_position(text)
    if explicit:
        unique=set(explicit)
        if len(unique)!=1 or not unique<=set(ids):raise ConstraintConflict('max_price','比较对象不在该任务已展示的商品中')
        pid=next(iter(unique))
    elif position is not None:
        if position<1 or position>len(ids):raise ConstraintConflict('max_price','所选商品位置超出已展示清单')
        pid=ids[position-1]
    elif len(ids)==1:pid=ids[0]
    else:raise ConstraintConflict('max_price','此前展示了多件商品，请说明要和哪一件比较（编号或第几款）')
    prices={};calls={}
    for evidence in task_evidence(previous,messages):
        data=evidence.data
        if evidence.tool not in {'search_products','query_products','get_product_info'} or data.get('success') is not True:continue
        rows=list(data.get('products',[]))
        if isinstance(data.get('product'),dict):rows.append(data['product'])
        for row in rows:
            if row.get('product_id')!=pid:continue
            try:price=Decimal(str(row['list_price']))
            except (KeyError,InvalidOperation,ValueError):continue
            if not price.is_finite() or price<=0:continue
            prices[price]=True;calls[price]=evidence.call_id
    if len(prices)!=1:raise ConstraintConflict('max_price','缺少唯一、有效的已展示商品目录价格证据')
    price=next(iter(prices));quantum=Decimal('0.0001')
    if price!=price.quantize(quantum):raise ConstraintConflict('max_price','商品价格精度与当前目录不一致，无法可靠比较')
    value=price+({'lt':-quantum,'gt':quantum}.get(direction,Decimal(0)))
    if Decimal(str(float(value)))!=value:raise ConstraintConflict('max_price','价格超出当前筛选参数可精确表示的范围')
    field='max_price' if direction in {'lt','le'} else 'min_price'
    return {'field':field,'value':float(value),'reference_product_id':pid,'reference_price':str(price),
        'reference_call_id':calls[price],'relation':direction,'source_index':index,'source_text':text}


def grounded_item(item,previous,request):
    """Leave numeric relation arithmetic to the program; keep explicit raw budgets."""
    updates=[]
    facts,clears=recognized(request[2])
    for update in item.field_updates:
        if update.field in {'min_price','max_price'}:
            # An explicit numeric budget still goes through the original validator.
            if update.field not in facts and update.field not in clears:continue
        if update.field in {'sort_by','sort_order','operation'}:
            if previous.kind=='product_search' or (update.field not in facts and update.field not in clears):continue
        updates.append(update)
    parameters=item.parameters.model_copy(update={'product_ids':[], 'product_position':None})
    return item.model_copy(update={'task_type':previous.kind,'next':previous.agent,'continuation':True,
        'target_task_id':previous.task_id,'parameters':parameters,'field_updates':updates})


def apply_bound(task,bound):
    # Replace an older derived comparison, while retaining the customer's actual
    # budget. Current raw budget changes have already replaced its provenance.
    params=task.parameters
    base_filter=params.catalog_filter
    for key in ('min_price','max_price'):
        source=task.parameter_sources.get(key,{})
        if source.get('verified_by')=='displayed_tool_price':
            if source.get('catalog_base_filter') is not None:
                from catalog_query import expression
                base_filter=expression(source['catalog_base_filter'])
            params=params.model_copy(update={key:source.get('base_bound')})
            if source.get('base_source'):task.parameter_sources[key]=source['base_source']
            else:task.parameter_sources.pop(key,None)
    field=bound['field'];old=getattr(params,field);value=bound['value']
    value=(min(old,value) if field=='max_price' else max(old,value)) if old is not None else value
    proposed=params.model_copy(update={field:value,'product_ids':[],'product_position':None})
    if proposed.min_price is not None and proposed.max_price is not None and proposed.min_price>proposed.max_price:
        raise ConstraintConflict(field,'相对价格与已保留的价格范围冲突，请说明是否调整原预算')
    task.parameters=proposed;task.displayed_product_ids=[];task.candidate_product_ids=[]
    if base_filter is not None:
        from catalog_query import CatalogExpression,CatalogGroup,CatalogPredicate
        comparison=CatalogPredicate(field='price',operator='lte' if field=='max_price' else 'gte',values=[value])
        branches=[g.conditions for g in base_filter.groups] or [[]]
        proposed.catalog_filter=CatalogExpression(groups=[CatalogGroup(conditions=group+[comparison]) for group in branches])
    base_source=task.parameter_sources.get(field)
    task.parameter_sources[field]={**bound,'base_bound':old,'base_source':base_source,
        'operation':'set','revision':task.revision,'verified_by':'displayed_tool_price',
        **({'catalog_base_filter':base_filter.model_dump(mode='json')} if base_filter is not None else {})}
    task.parameter_changes=(task.parameter_changes+[{'field':field,'action':'relative_price','old':old,'new':value,
        **{k:v for k,v in bound.items() if k not in {'field','value'}}}])[-30:]
    return task
