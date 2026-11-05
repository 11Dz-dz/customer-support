"""Customer-grounded updates for catalog/recommendation/quote constraints.

The planner proposes changes; this module owns merge and evidence validation.
This is a bounded business vocabulary, not a universal natural-language proof.
Unrecognized proposed hard constraints require clarification before tools run.
"""
import re
from decimal import Decimal
from task_state import TaskParameters, explicit_product_count

FIELDS=('product_category','min_price','max_price','color','size','limit','quantity',
        'include_variants','include_zero_price','sort_by','sort_order','operation','quote_grouping')
KINDS={'product_search','catalog_query','product_detail','quote'}

def fields_for_kind(kind):
    if kind=='quote':return {'quantity','quote_grouping'}
    if kind=='product_detail':return {'min_price','max_price'}
    fields=set(FIELDS)-{'quantity','quote_grouping'}
    return fields-{'sort_by','sort_order','operation'} if kind=='product_search' else fields

LABELS={'product_category':'商品类别','min_price':'最低价格','max_price':'最高价格','color':'颜色',
        'size':'尺寸','limit':'展示数量','quantity':'购买数量','include_variants':'同型号规格',
        'include_zero_price':'零标价选项','sort_by':'排序字段','sort_order':'排序方向',
        'operation':'查询方式','quote_grouping':'报价方式'}
LABELS['catalog_filter']='商品筛选条件'
CATEGORY_TERMS={
    'bicycles':['自行车','单车','整车','山地车','公路车','bicycle','bicycles','mountain bike','road bike'],
    'components':['自行车车把','自行车车座','自行车车架','车把','车座','车架','踏板','链条','零部件','零件','把手','车轮','handlebar','frame','saddle','component'],
    'clothing':['自行车骑行服','骑行服','骑行裤','球衣','运动衫','服装','袜子','jersey','shorts','clothing'],
    'accessories':['自行车轮胎','轮胎','内胎','tire','tyre','tube','自行车头盔','骑行头盔','头盔','水壶','水瓶','水壶架','打气筒','背包','装备','helmet','bottle','pump','accessories'],
}
NUMBER=r'(?:\d+(?:[,.]\d+)*|[一二两三四五六七八九十百千万零]+)'
NEGATIVE=re.compile(r'(?:不要|不买|不看|排除|不是|不推荐|不选)\s*$')


FIELD_CUES={'product_category':r'类别|自行车|单车|整车|头盔|车把|换|改成|换成',
             'min_price':r'预算|价格|钱|元|上限|下限|价', 'max_price':r'预算|价格|钱|元|上限|下限|价',
             'color':r'色|color', 'size':r'尺寸|尺码|size','limit':r'款|种|个|展示|推荐数量',
             'quantity':r'件|辆|买|购买数量','include_variants':r'型号|规格|不同颜色|不同尺寸',
             'include_zero_price':r'零标价|0标价','sort_by':r'排序|最贵|便宜|最高价|最低价|按',
             'sort_order':r'排序|最贵|便宜|最高价|最低价|从高|从低|升序|降序',
             'operation':r'多少|统计|计数|列出|展示|有哪些|列表','quote_grouping':r'报价|分开|合计|合并|一共'}

class ConstraintConflict(ValueError):
    def __init__(self,field,reason,*,repairable=False):
        self.field=field;self.reason=reason;self.repairable=repairable
        super().__init__('需要确认'+LABELS.get(field,field)+'：'+reason+'。请说明本次要修改的条件；原任务条件已保留。')


def number(token):
    if re.fullmatch(r'\d+(?:[,.]\d+)*',token):return float(token.replace(',',''))
    digits=dict(zip('零一二三四五六七八九',range(10)));digits['两']=2
    total=section=current=0
    for c in token:
        if c in digits:current=digits[c]
        elif c in '十百千':section+=(current or 1)*{'十':10,'百':100,'千':1000}[c];current=0
        elif c=='万':total+=(section+current)*10000;section=current=0
        else:raise ValueError('unsupported numeral')
    return float(total+section+current)


def category_mentions(text):
    text=text.lower();matches=[]
    for category,terms in CATEGORY_TERMS.items():
        for term in terms:
            for m in re.finditer(re.escape(term),text):
                if term.isascii() and ((m.start() and text[m.start()-1].isalpha()) or
                                      (m.end()<len(text) and text[m.end()].isalpha())):continue
                matches.append((m.start(),m.end(),category))
    for m in re.finditer(r'(?:公路|山地|旅行|竞赛)(?:竞赛|旅行)?(?:自行)?车',text):matches.append((m.start(),m.end(),'bicycles'))
    longest=[m for m in matches if not any(n[0]<=m[0] and n[1]>=m[1] and n[:2]!=m[:2] for n in matches)]
    # In “公路车内胎” the bicycle noun qualifies the accessory head. Conjoined
    # independent objects such as “自行车和头盔” retain both scopes.
    longest=[m for m in longest if not (m[2]=='bicycles' and any(
        n[2]!='bicycles' and m[0]<=n[0]<=m[1]+4 and n[1]>=m[1] and
        not re.search(r'和|与|及|或|再|还有|[，,。；;]',text[m[1]:n[0]]) for n in longest))]
    return [(a,b,c) for a,b,c in longest if not NEGATIVE.search(text[max(0,a-6):a])]


def recognized(text):
    """Return only explicit, locally verifiable field operations in one source span."""
    values={};clear=set();low=text.lower()
    cats={m[2] for m in category_mentions(text)}
    if len(cats)==1:values['product_category']=next(iter(cats))
    if re.search(r'类别不限|不限类别|不限商品类别|不限制类别|所有类别|整车和配件都可以|整车配件都可以',text):values['product_category']='all'
    count=explicit_product_count(text)
    if count is not None:values['limit']=count
    english=re.search(r'\b(?:recommend|show|list|find)\s+(one|two|three|\d+)\s+(?:products?|bicycles?|bikes?|items?)\b',low)
    if english:values['limit']={'one':1,'two':2,'three':3}.get(english[1],int(english[1]) if english[1].isdigit() else 1)
    top=re.search(rf'(?:前|最贵的?|最便宜的?|(?:标价|价格)(?:最高|最低)的?)\s*({NUMBER})\s*(?:款|个|种|件|条)?',text)
    if top:values['limit']=int(number(top[1]))
    elif count is None and re.search(r'最贵|最便宜|价格最高的|价格最低的|标价最高|标价最低',text):values['limit']=1
    def amount(field,patterns):
        found={number(m[1]) for pattern in patterns for m in re.finditer(pattern,text,re.I)
               if not re.match(r'\s*(?:款|个|件|辆|种)',text[m.end():])}
        if len(found)==1:values[field]=next(iter(found))
    amount('max_price',[rf'(?:预算|最高(?:价格|标价)?|不超(?:过)?|别超(?:过)?|不高于|最多|上限|budget)\s*(?:是|为|提高到|降低到|调整为|改为|改成|到|[:：])?\s*({NUMBER})',
                        rf'({NUMBER})\s*(?:元|块|人民币)?\s*(?:以内|以下|封顶)'])
    amount('min_price',[rf'(?:最低(?:价格|标价)?|不低于|不少于|至少|下限)\s*(?:是|为|[:：])?\s*({NUMBER})',
                        rf'({NUMBER})\s*(?:元|块)?\s*(?:以上|起步)'])
    if re.search(r'大于\s*0(?:\D|$)|高于\s*0(?:\D|$)',text):
        values['min_price']=0.0;values['include_zero_price']=False
    ranges=list(re.finditer(rf'({NUMBER})\s*(?:元)?\s*(?:到|至|[-~～])\s*({NUMBER})\s*(?:元|之间|以内|范围)',text))
    if len(ranges)==1:values.update(min_price=number(ranges[0][1]),max_price=number(ranges[0][2]))
    if re.search(r'不限预算|预算不限|不限价格|价格不限|不限制价格|取消预算|不要沿用刚才预算|取消价格限制|去掉价格条件',text):clear.update(['min_price','max_price'])
    if re.search(r'不设上限|取消最高价|去掉最高价',text):clear.add('max_price')
    if re.search(r'取消最低价|去掉最低价|不设下限',text):clear.add('min_price')
    colors=[]
    for m in re.finditer(r'红色|黑色|蓝色|白色|黄色|银色|灰色|多色|\b(?:red|black|blue|white|yellow|silver|grey|gray|multi)\b',text,re.I):
        if not NEGATIVE.search(text[max(0,m.start()-6):m.start()]):colors.append(TaskParameters(color=m[0]).color)
    if len(set(colors))==1:values['color']=colors[0]
    if re.search(r'不限颜色|颜色不限|取消颜色|去掉颜色|任意颜色',text):clear.add('color')
    sizes=re.findall(r'(?:尺寸|尺码|size)\s*(?:为|是|改成|改为|[:：])?\s*([A-Za-z0-9.-]+)',text,re.I)
    sizes+=re.findall(r'(?<![0-9])([0-9]{1,3})\s*码',text)
    if len(set(sizes))==1:values['size']=sizes[0]
    if re.search(r'不限尺寸|不限尺码|尺寸不限|尺码不限|取消尺寸|取消尺码',text):clear.add('size')
    quantities={int(number(m[1])) for m in re.finditer(rf'({NUMBER})\s*(?:件|辆|台)',text)}
    qty=re.search(rf'(?:每款|每种|各|购买|买|订购|改成|改为)\s*(?:买|购买)?\s*({NUMBER})\s*(?:件|辆|个|台)',text)
    if len(quantities)==1:values['quantity']=next(iter(quantities))
    elif qty and not quantities:values['quantity']=int(number(qty[1]))
    if qty and re.search(r'每款|每种|各',qty[0]):values['quote_grouping']='separate'
    if re.search(r'同型号|不同规格|不同尺寸|不同颜色',text):
        values['include_variants']=not bool(re.search(r'不要同型号|不看同型号|排除同型号|不含同型号|不要不同规格',text))
    if re.search(r'不同型号|每个型号一款|不重复型号',text):values['include_variants']=False
    if re.search(r'包含零标价|包括零标价|包含0标价|包括0标价',text):values['include_zero_price']=True
    if re.search(r'排除零标价|排除0标价|不要零标价|不要0标价|只看正标价',text):values['include_zero_price']=False
    if re.search(r'最贵|最高价|标价最高|价格最高|价格降序|标价降序|从高到低',text):values.update(sort_by='price',sort_order='desc')
    if re.search(r'最便宜|最低价商品|标价最低|价格最低|价格升序|标价升序|从低到高',text):values.update(sort_by='price',sort_order='asc')
    if re.search(r'按名称|名称排序',text):values['sort_by']='name'
    if re.search(r'按编号|编号排序',text):values['sort_by']='product_id'
    if re.search(r'升序',text):values['sort_order']='asc'
    if re.search(r'降序',text):values['sort_order']='desc'
    if re.search(r'多少款|多少种|多少条|(?:记录|商品|款式).{0,5}(?:有多少|多少)|数量|统计数量|计数',text) and not qty:values['operation']='count'
    if re.search(r'最高价.*最低价|价格分布|价格统计|平均价',text):values['operation']='stats'
    if re.search(r'列出|展示|看看|有哪些|列表',text) and 'operation' not in values:values['operation']='list'
    if re.search(r'分别报价|分别算|分开报价|各自报价',text):values['quote_grouping']='separate'
    if re.search(r'一起(?:报价|算)|合并(?:报价|算|计算|小计)|一共多少钱|合计(?:多少钱|标价|小计|价格)',text):values['quote_grouping']='combined'
    for key in clear:values.pop(key,None)
    return values,clear


def may_repair_proposal(conflict,inputs):
    """Never let repair erase an explicitly requested but unsupported condition."""
    if not conflict.repairable:return False
    if conflict.field=='catalog_filter':return True
    if conflict.field in {'operation','quantity','quote_grouping'}:return True
    if conflict.field == 'limit':
        from task_state import presentation_count_matches
        # A classifier word alone (换个/这个) does not establish a count.
        # Exact numeric field validation below remains authoritative.
        return not any(presentation_count_matches(text) for text in inputs) or any(
            'limit' in recognized(text)[0] for text in inputs)
    for text in inputs:
        facts,clears=recognized(text)
        if conflict.field in facts or conflict.field in clears:return True
    return not any(re.search(FIELD_CUES[conflict.field],text,re.I) for text in inputs)


def _source_clause(text,quote):
    # Check the whole containing clause, so quoting just “自行车” cannot remove “不要”.
    parts=re.split(r'[，,。；;！!？?\n]',text)
    matches=[p for p in parts if quote in p]
    return matches[0] if len(matches)==1 else text


def _equal(a,b):
    if type(a) is bool or type(b) is bool:return type(a) is type(b) and a==b
    if isinstance(a,(int,float)) and isinstance(b,(int,float)):return Decimal(str(a))==Decimal(str(b))
    return a==b


def apply_constraint_updates(task,item,previous,inputs,*,single_scope=True):
    """Atomic approved-field merge; caller commits no task when any change conflicts."""
    transitioned=False
    if (previous and previous.kind=='catalog_query' and task.kind=='catalog_query'
            and previous.task_id==task.task_id and previous.parameters.operation=='coverage'
            and not item.cancel and not item.retry_failed):
        filter_fields={'product_category','min_price','max_price','color','size'}
        has_filter=(item.query_update is not None or any(u.field in filter_fields for u in item.field_updates)
                    or single_scope and any(filter_fields & set(recognized(text)[0]) for text in inputs))
        if has_filter:
            from catalog_coverage import object_filter
            base=object_filter(previous.parameters.requested_item)
            if base is None:
                raise ConstraintConflict('catalog_filter','尚未确认该商品类型，请指定目录中可核对的商品后再筛选')
            previous=previous.model_copy(deep=True)
            previous.parameters.operation='list'
            previous.parameters.catalog_filter=base
            item=item.model_copy(deep=True)
            if item.parameters.operation in {None,'coverage'}:item.parameters.operation='list'
            task.parameters.operation=item.parameters.operation
            transitioned=True
    if task.kind=='catalog_query' and item.parameters.operation=='coverage' and not item.cancel and not item.retry_failed:
        task.parameters=TaskParameters(operation='coverage',requested_item=item.parameters.requested_item,search_terms=item.parameters.search_terms)
        return task
    if task.kind not in KINDS or item.cancel or item.retry_failed:return task
    same=previous is not None and previous.kind==task.kind and task.task_id==previous.task_id
    original=previous.parameters.model_dump() if same else TaskParameters().model_dump()
    if previous and not same and previous.agent=='sales':
        from task_state import product_handoff_fields
        original.update(previous.parameters.model_dump(include=product_handoff_fields(previous.kind,task.kind)))
    proposed=task.parameters.model_dump();approved={};sources={};audit=[]
    if transitioned:audit.append({'field':'operation','action':'coverage_to_list','old':'coverage','new':'list'})
    applicable=fields_for_kind(task.kind)
    overview=task.kind=='catalog_query' and item.parameters.operation=='overview'
    if overview:applicable=applicable-{'operation'}
    if getattr(item,'query_update',None) is not None:
        applicable=applicable-{'product_category','color','size','min_price','max_price'}
    updates=[u for u in item.field_updates if u.field in applicable]
    explicit_keys={u.field for u in updates}
    if len({(u.field,u.source_index) for u in updates})!=len(updates):raise ConstraintConflict('product_category','同一条消息对同一字段给出了多个修改')
    for update in sorted(updates,key=lambda u:u.source_index):
        key=update.field
        # Repeating an already-approved value is not a new edit. Do not demand
        # a fresh quote for it or overwrite its original provenance.
        no_op=update.operation=='clear' and original[key] is None
        if update.operation=='set':
            try:no_op=_equal(getattr(TaskParameters.model_validate({key:update.value}),key),original[key])
            except ValueError:no_op=False
        if same and no_op:
            audit.append({'field':key,'action':'ignored_unchanged_patch'})
            explicit_keys.discard(key)
            continue
        if update.source_index>=len(inputs) or update.source_text not in inputs[update.source_index]:raise ConstraintConflict(key,'找不到这项修改对应的用户原文',repairable=True)
        clause=_source_clause(inputs[update.source_index],update.source_text)
        facts,clears=recognized(clause)
        cats={m[2] for m in category_mentions(clause)}
        wanted=proposed.get('product_category')
        if not single_scope and cats and wanted not in cats and key!='product_category':raise ConstraintConflict(key,'这段原文可能属于另一项商品任务')
        if same and key not in facts and key not in clears and not re.search(FIELD_CUES[key],clause,re.I):
            # A model's unrelated extra edit must not block another grounded edit.
            # Preserve the old field; do not relax the missing evidence requirement.
            audit.append({'field':key,'action':'ignored_unrelated_patch','source_index':update.source_index})
            explicit_keys.discard(key)
            continue
        if update.operation=='clear':
            if key not in clears or update.value is not None:raise ConstraintConflict(key,'原文未明确要求清除这项条件')
            value=None
        else:
            try:value=getattr(TaskParameters.model_validate({key:update.value}),key)
            except ValueError:raise ConstraintConflict(key,'新值的类型或范围无效',repairable=True) from None
            read_mode=key=='operation' and task.kind=='catalog_query' and not same and key not in facts and value in {'list','count'}
            if not read_mode and (key not in facts or not _equal(facts[key],value)):raise ConstraintConflict(key,'提交的新值与原始表述无法一致核对',repairable=True)
        approved[key]=value
        sources[key]={'source_index':update.source_index,'source_text':update.source_text,
                      'source_message_text':inputs[update.source_index],'operation':update.operation,'revision':task.revision}
        if key=='operation' and key not in facts:sources[key]['verified_by']='schema_validated_read_mode'
    # Compatibility for old adapters: only independently recognized raw-user fields
    # can amend constraints. Never derive these from item.query or its defaults.
    if single_scope:
        for index,text in enumerate(inputs):
            # Whole-message extraction is intentionally skipped for mixed objects;
            # scoped evidence is required in that case.
            if len({m[2] for m in category_mentions(text)})>1:continue
            facts,clears=recognized(text)
            for key,value in {**facts,**dict.fromkeys(clears)}.items():
                if key not in applicable:continue
                if key in explicit_keys:
                    # A later raw explicit change must not be replaced by earlier cited evidence.
                    if index>sources[key]['source_index'] and not _equal(value,approved[key]):raise ConstraintConflict(key,'更新引用了旧消息，遗漏了用户后续的明确修改')
                    continue
                # Catalog-query operations do not alter recommendation selection semantics.
                if key in {'sort_by','sort_order','operation'} and task.kind!='catalog_query':continue
                if key=='quantity' and task.kind!='quote':continue
                if key=='limit' and task.kind=='quote':continue
                approved[key]=value;sources[key]={'source_index':index,'source_text':text,'source_message_text':text,
                    'operation':'clear' if key in clears else 'set','revision':task.revision,'verified_by':'business_normalizer'}
    for key in FIELDS:
        if key=='operation' and overview:
            proposed[key]='overview'
            continue
        if key not in applicable:
            proposed[key]=original[key]
            continue
        requested=proposed[key]
        if key in approved:value=approved[key]
        elif same:value=original[key]
        elif key=='product_category' and requested in (None,'all') and not any(category_mentions(t) for t in inputs):value=requested
        elif key=='limit' and requested in (None,5):value=requested
        elif task.kind=='catalog_query' and (key,requested) in [('sort_by','price'),('sort_order','asc'),('operation','list'),('include_zero_price',False)]:value=requested
        elif not same and task.kind=='catalog_query' and key=='operation' and requested=='count':value=requested
        elif task.kind=='quote' and key=='quote_grouping' and requested=='combined':value=requested
        elif requested==original[key]:value=requested
        else:raise ConstraintConflict(key,'这项新条件缺少可核对的用户依据',repairable=True)
        proposed[key]=value
        if not _equal(requested,value):audit.append({'field':key,'action':'ignored_ungrounded_proposal','proposed':requested,'kept':value})
        if key in approved:
            audit.append({'field':key,'action':sources[key]['operation'],'old':original[key],'new':value,
                          'source_index':sources[key]['source_index'],'source_text':sources[key]['source_text']})
    proposed['clear_fields']=[]
    if task.kind=='product_search':
        proposed['limit']=proposed['limit'] or 5
        proposed['product_category']=proposed['product_category'] or 'all'
    if proposed['min_price'] is not None and proposed['max_price'] is not None and proposed['min_price']>proposed['max_price']:
        raise ConstraintConflict('max_price','最低价格高于最高价格')
    if task.kind=='product_search' and any(k in approved and not _equal(original[k],approved[k]) for k in ('product_category','min_price','max_price','color','size','limit','include_variants')):
        proposed['product_ids']=[]
        task.displayed_product_ids=[];task.candidate_product_ids=[]
    task.parameters=TaskParameters.model_validate(proposed)
    task.parameter_sources.update(sources);task.parameter_changes=audit[:30]
    from catalog_query import bind_update
    if task.kind in {'product_search','catalog_query','product_detail'}:
        task=bind_update(task,item,previous,inputs,approved,single_scope=single_scope)
        if item.query_update is None and task.kind in {'product_search','catalog_query'}:
            from product_taxonomy import normalize_object,object_only_keyword
            from catalog_query import effective
            # Legacy plans still use the same taxonomy; only the task's cited query
            # is eligible in a multi-object turn, never another task's conditions.
            source='\n'.join(inputs) if single_scope else next((t for t in inputs if item.query and item.query in t),'')
            if source:
                from catalog_query import fields,amend,CatalogExpression
                patch=normalize_object(CatalogExpression(),source)
                if 'product_type' in fields(patch):
                    task.parameters.catalog_filter=amend(effective(task.parameters.catalog_filter,
                        **task.parameters.model_dump(include={'min_price','max_price','product_category','color','size'})),patch)
                    # A named type owns its parent category too; scalar mirrors
                    # may not reintroduce the old incompatible category.
                    for c in patch.groups[0].conditions:
                        if c.field=='category' and len(c.values)==1:task.parameters.product_category=c.values[0]
                if task.kind=='catalog_query' and object_only_keyword(task.parameters.keyword,task.parameters.catalog_filter):task.parameters.keyword=None
    return task


def validate_shared_sources(items):
    """A bare shared budget cannot silently update several independent products."""
    seen={}
    relevant=[i for i in items if i.task_type in KINDS and not i.cancel]
    if len(relevant)<2:return
    for item in relevant:
        update=getattr(item,'query_update',None)
        if update is not None:
            from catalog_query import fields
            for field in fields(update.expression)|set(update.clear_fields):
                key=(field,update.source_index,update.source_text)
                if key in seen and seen[key]!=item.ref and not re.search(r'都|每项|各自|两项|全部|分别',update.source_text):
                    raise ConstraintConflict('catalog_filter','同一段条件被分配给多项商品任务，请分别引用各自的用户条件')
                seen[key]=item.ref
        for u in item.field_updates:
            if u.field not in fields_for_kind(item.task_type):continue
            key=(u.field,u.source_index,u.source_text)
            if key in seen and seen[key]!=item.ref and not re.search(r'都|每项|各自|两项|全部|分别',u.source_text):
                raise ConstraintConflict(u.field,'同一段条件被分配给多项任务，需要明确分别应用到哪一项')
            seen[key]=item.ref


def customer_changes_saved_constraints(item,old,inputs):
    """A confirmed new constraint is an amendment, never a replay of failed reads."""
    if old is None or old.kind not in KINDS or item.cancel:return False
    for text in inputs:
        facts,clears=recognized(text)
        if any(k in fields_for_kind(old.kind) and not _equal(getattr(old.parameters,k),v) for k,v in {**facts,**dict.fromkeys(clears)}.items()):return True
    return False


def guard_ambiguous_target(items,old,inputs):
    if len(inputs)!=1:return
    text=inputs[0]
    facts,clears=recognized(text)
    keys=set(facts)|clears
    active=[t for t in old if t.kind in KINDS and t.status!='cancelled' and fields_for_kind(t.kind)&keys]
    if len(active)<2 or category_mentions(text) or re.search(r'第[一二三四1234]项|第[一二三四1234]个',text):return
    if any(t.task_id in text for t in active):return
    facts,clears=recognized(text)
    if (facts or clears) and any(i.continuation or i.target_task_id for i in items):
        raise ConstraintConflict(next(iter(facts or clears)),'有多项商品任务，请说明这次条件修改哪一项')


def collapse_unpublished_replacement(decision,old,inputs):
    """Merge an explicit replacement chain before any task in a new segment exists.

    This never rebinds existing/multiple stored tasks or deletes dependencies.
    Uncertain or independent object scopes are left for ordinary validation.
    """
    if old or len(inputs)<2 or len(decision.tasks)<2:return decision,False
    items=decision.tasks
    if any(i.task_type!='product_search' or i.cancel or i.retry_failed or i.target_task_id or i.depends_on for i in items):return decision,False
    if any(re.search(r'另外|另一个|此外|同时|都要|也要|再推荐|分别|独立',text) for text in inputs):return decision,False
    first={m[2] for m in category_mentions(inputs[0])}
    if len(first)!=1:return decision,False
    initial=current=next(iter(first));changed=False
    for text in inputs[1:]:
        cats={m[2] for m in category_mentions(text)}
        if len(cats)>1:return decision,False
        if cats and next(iter(cats))!=current:
            if not re.match(r'^\s*(?:(?:不看|不要|不买)[^，,。；;]*[，,。；;]\s*)?(?:换成|换为|改成|改为)',text):return decision,False
            current=next(iter(cats));changed=True
    if not changed or current==initial:return decision,False
    candidates=[i for i in items if i.parameters.product_category==current]
    categories={i.parameters.product_category for i in items}
    if len(candidates)!=1 or categories!={initial,current}:return decision,False
    # Original user facts are validated again by apply_constraint_updates below.
    # Only this single target is retained; earlier count/budget remain raw input.
    return decision.model_copy(update={'tasks':[candidates[0].model_copy(deep=True)],'clarification':None}),True
