"""Bounded catalog predicate language shared by SQL, retrieval and answer checks.

OR across groups, AND within a group. IN/NOT IN express alternatives/negation.
No SQL, field names, or arbitrary expressions are accepted from a model.
"""
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator,field_validator
from backend.catalog.catalog_filters import CATEGORY_SUBCATEGORIES

FilterField=Literal['category','product_type','color','size','price']
class CatalogPredicate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    field: FilterField
    operator: Literal['in','not_in','eq','ne','lt','lte','gt','gte']
    values: list[str|float] = Field(min_length=1,max_length=12)

    @field_validator('values',mode='before')
    @classmethod
    def reject_booleans(cls,values):
        if isinstance(values,list) and any(isinstance(v,bool) for v in values):raise ValueError('Boolean is not a catalog value')
        return values

    @model_validator(mode='after')
    def valid(self):
        if any(isinstance(v,bool) for v in self.values):raise ValueError('Boolean is not a catalog value')
        if self.field=='price':
            if self.operator in {'in','not_in'}:raise ValueError('Price requires a comparison')
            if len(self.values)!=1:raise ValueError('One comparison value required')
            value=Decimal(str(self.values[0]))
            if not value.is_finite() or value<0:raise ValueError('Invalid price')
            self.values=[float(value)]
        else:
            if self.operator not in {'in','not_in','eq','ne'}:raise ValueError('Invalid categorical comparison')
            if any(not isinstance(v,str) or not v.strip() or len(v)>80 for v in self.values):raise ValueError('Invalid categorical value')
            self.values=list(dict.fromkeys(v.strip() for v in self.values))
            if self.operator in {'eq','ne'} and len(self.values)!=1:raise ValueError('One comparison value required')
            if self.field=='category' and any(v not in CATEGORY_SUBCATEGORIES for v in self.values):raise ValueError('Unknown category')
            if self.field=='product_type':
                from backend.catalog.product_taxonomy import PRODUCT_TYPES
                if any(v not in PRODUCT_TYPES for v in self.values):raise ValueError('Unknown product type')
        return self

class CatalogGroup(BaseModel):
    model_config=ConfigDict(extra='forbid')
    conditions: list[CatalogPredicate] = Field(min_length=1,max_length=12,description='一个完整方案：其中全部条件必须同时成立，公共预算和类别不可遗漏')

class CatalogExpression(BaseModel):
    model_config=ConfigDict(extra='forbid')
    groups: list[CatalogGroup] = Field(default_factory=list,max_length=4,description='普通筛选只写一组；同字段的或者用一个in列表。只有明确跨字段的完整备选方案才分多组；每组必须重复公共条件')

    @model_validator(mode='after')
    def bounded(self):
        if sum(len(g.conditions) for g in self.groups)>24:raise ValueError('Too many predicates')
        return self

class CatalogUpdate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    operation: Literal['replace','amend','clear']='amend'
    expression: CatalogExpression=Field(default_factory=CatalogExpression)
    clear_fields: list[FilterField]=Field(default_factory=list,max_length=5)
    source_index: int=Field(ge=0)
    source_text: str=Field(min_length=1,max_length=1000)

def expression(value=None):
    return value if isinstance(value,CatalogExpression) else CatalogExpression.model_validate(value or {})

def fields(value):return {c.field for g in expression(value).groups for c in g.conditions}

def amend(old,new,clear=()):
    old=expression(old);new=expression(new);changed=fields(new)|set(clear)
    # Removing a field from a disjunction must keep an empty branch as TRUE.
    base=[[c for c in g.conditions if c.field not in changed] for g in old.groups] or [[]]
    if any(not branch for branch in base):base=[[]]
    additions=[g.conditions for g in new.groups] or [[]]
    groups=[a+b for a in base for b in additions]
    if any(not branch for branch in groups):return CatalogExpression()
    return CatalogExpression(groups=[CatalogGroup(conditions=branch) for branch in groups])

def effective(value=None,*,min_price=None,max_price=None,product_category=None,color=None,size=None):
    value=expression(value);owned=fields(value);legacy=[]
    for f,v in [('category',product_category),('color',color),('size',size)]:
        if f not in owned and v is not None and v!='all':legacy.append(CatalogPredicate(field=f,operator='eq',values=[v]))
    if 'price' not in owned:
        for op,v in [('gte',min_price),('lte',max_price)]:
            if v is not None:legacy.append(CatalogPredicate(field='price',operator=op,values=[v]))
    if not legacy:return value
    return CatalogExpression(groups=[CatalogGroup(conditions=g.conditions+legacy) for g in value.groups] or [CatalogGroup(conditions=legacy)])

def sql(value=None,*,alias=''):
    if alias not in {'','p.'}:raise ValueError('Unknown SQL alias')
    groups=[];args=[]
    for group in expression(value).groups:
        clauses=[]
        for c in group.conditions:
            if c.field in {'category','product_type'}:
                from backend.catalog.product_taxonomy import PRODUCT_TYPES
                mapping=CATEGORY_SUBCATEGORIES if c.field=='category' else {k:v[0] for k,v in PRODUCT_TYPES.items()}
                ids=sorted({n for v in c.values for n in mapping[v]});column=alias+'product_subcategory_id'
                clause=column+' = ANY(%s)';args.append(ids)
            else:
                column=alias+{'price':'list_price','color':'color','size':'size'}[c.field]
                if c.field=='price':
                    op={'eq':'=','ne':'<>','lt':'<','lte':'<=','gt':'>','gte':'>='}[c.operator]
                    clause=column+' '+op+' %s';args.append(Decimal(str(c.values[0])))
                else:
                    clause='lower('+column+') = ANY(%s)';args.append([v.lower() for v in c.values])
            if c.operator in {'not_in','ne'} and c.field!='price':clause='NOT ('+clause+')'
            clauses.append('('+column+' IS NOT NULL AND '+clause+')')
        groups.append('('+' AND '.join(clauses)+')')
    return ('('+' OR '.join(groups)+')' if groups else 'TRUE'),args

def matches(product,value=None):
    def match(c):
        actual=product.get({'category':'product_subcategory_id','product_type':'product_subcategory_id','price':'list_price'}.get(c.field,c.field))
        if actual is None:return False
        if c.field=='price':
            a,b=Decimal(str(actual)),Decimal(str(c.values[0]))
            return {'eq':a==b,'ne':a!=b,'lt':a<b,'lte':a<=b,'gt':a>b,'gte':a>=b}[c.operator]
        if c.field in {'category','product_type'}:
            from backend.catalog.product_taxonomy import PRODUCT_TYPES
            mapping=CATEGORY_SUBCATEGORIES if c.field=='category' else {k:v[0] for k,v in PRODUCT_TYPES.items()}
            ok=actual in {n for v in c.values for n in mapping[v]}
        else:ok=str(actual).lower() in [v.lower() for v in c.values]
        return not ok if c.operator in {'not_in','ne'} else ok
    groups=expression(value).groups
    return not groups or any(all(match(c) for c in g.conditions) for g in groups)

def capabilities():
    from backend.catalog.product_taxonomy import PRODUCT_TYPES
    return {'categories':{'bicycles':'自行车整车（山地/公路/旅行车）','components':'自行车零部件','clothing':'骑行服装','accessories':'骑行装备'},
        'category_groups':{'自行车配件':['components','accessories']},
        'transport_categories':['bicycles'],'unknown_category_policy':'未分类记录不推断用途；类别覆盖不等于实时库存',
        'product_types':{k:v[1] for k,v in PRODUCT_TYPES.items()},
        'filter_fields':['category','product_type','color','size','price'],'logic':'groups之间OR，conditions之间AND；in/not_in支持多个备选值',
        'query_modes':['overview目录范围','list/count/stats精确查询','product_search用途推荐','product_variants同款规格']}

def impossible(value):
    """Prove simple contradictions without inventing catalog availability."""
    def branch_false(group):
        for field in ('category','product_type','color','size'):
            allowed=None;denied=set()
            for c in group.conditions:
                if c.field!=field:continue
                values={str(v).casefold() for v in c.values}
                if c.operator in {'eq','in'}:allowed=values if allowed is None else allowed&values
                else:denied|=values
            if allowed is not None and not allowed-denied:return True
        lower=(Decimal(0),True);upper=None;denied_prices=set()
        for c in group.conditions:
            if c.field!='price':continue
            v=Decimal(str(c.values[0]));op=c.operator
            if op=='ne':denied_prices.add(v)
            if op in {'gt','gte','eq'}:
                candidate=(v,op!='gt')
                if v>lower[0] or v==lower[0] and not candidate[1]:lower=candidate
            if op in {'lt','lte','eq'}:
                candidate=(v,op!='lt')
                if upper is None or v<upper[0] or v==upper[0] and not candidate[1]:upper=candidate
        return upper is not None and (lower[0]>upper[0] or lower[0]==upper[0] and (not(lower[1] and upper[1]) or lower[0] in denied_prices))
    groups=expression(value).groups
    return bool(groups) and all(branch_false(g) for g in groups)

def check_positive_scope(value,text):
    """Catch dropped explicit positive scopes; never infer/replace a field value.

    This is a bounded completeness check, not a general semantic proof. Negative
    clauses remain model-interpreted and are never turned into positive filters.
    """
    import re
    from backend.tasks.constraint_updates import recognized,ConstraintConflict
    required=set()
    for m in re.finditer(r'(?:只看|只要|仅看|仅限|只选)\s*([^，,。；;]+)',text):
        clause=m[1]
        if re.match(r'(?:不要|不是|不含|排除|非|除了|除.*(?:以外|之外))',clause):continue
        facts,_=recognized(clause)
        for key,field in [('product_category','category'),('color','color'),('size','size')]:
            if key in facts and facts[key] not in (None,'all'):required.add(field)
    for group in expression(value).groups:
        present={c.field for c in group.conditions if c.operator in {'eq','in'}}
        if required-present:
            raise ConstraintConflict('catalog_filter','原文有明确的正向商品范围，但条件仅保留排除项或遗漏该范围；需同时保留包含条件和排除条件',repairable=True)

def bind_update(task,item,previous,inputs,legacy_changes,*,single_scope=False):
    """Model owns semantic operators; program validates provenance and patch scope."""
    from backend.tasks.constraint_updates import ConstraintConflict
    update=item.query_update
    from backend.tasks.task_state import product_handoff_fields
    old=previous.parameters if previous and (previous.kind==task.kind or product_handoff_fields(previous.kind,task.kind)) else None
    if update is None and item.parameters.catalog_filter is not None and (old is None or item.parameters.catalog_filter!=old.catalog_filter):
        raise ConstraintConflict('catalog_filter','新条件必须通过带用户原文的query_update提交，不能直接覆盖已存条件')
    base=effective(old.catalog_filter,**old.model_dump(include={'min_price','max_price','product_category','color','size'})) if old else CatalogExpression()
    if update is not None:
        import re
        if update.source_index>=len(inputs) or update.source_text not in inputs[update.source_index]:
            raise ConstraintConflict('catalog_filter','查询条件找不到对应的本轮用户原文',repairable=True)
        from backend.tasks.constraint_updates import _source_clause
        scope_text=_source_clause(inputs[update.source_index],update.source_text)
        def resets(text):return bool(re.search(r'(?:整个目录|全目录|全部商品)',text) and re.search(r'改查|改看|换成|重新|不再限|不限|取消|去掉',text))
        if single_scope and resets(inputs[update.source_index]):scope_text=inputs[update.source_index]
        reset=resets(scope_text)
        operation='amend' if old is not None and update.operation=='replace' and not reset else update.operation
        if update.source_index>=len(inputs) or update.source_text not in inputs[update.source_index]:
            raise ConstraintConflict('catalog_filter','查询条件找不到对应的本轮用户原文')
        from backend.tasks.constraint_updates import NUMBER,number,recognized
        from backend.catalog.product_taxonomy import normalize_object
        proposed=normalize_object(update.expression,scope_text,trusted=base)
        if reset:
            base=CatalogExpression();task.parameters.keyword=None;task.parameters.search_terms=[]
            task.parameter_changes.append({'field':'catalog_filter','action':'explicit_scope_reset','source_index':update.source_index})
        current,raw_clears=recognized(update.source_text)
        clears=set(update.clear_fields)|({k for k in raw_clears if k in {'color','size'}})
        if {'min_price','max_price'}<=raw_clears:clears.add('price')
        if current.get('product_category')=='all':clears.add('category')
        if 'category' in fields(proposed) and 'product_type' not in fields(proposed) and current.get('product_category') is not None:
            clears.add('product_type')
        if operation in {'amend','clear'} and len(base.groups)==1 and len(proposed.groups)==1:
            def field_signature(group,field):
                values=[]
                for c in group.conditions:
                    if c.field!=field:continue
                    op='in' if c.operator=='eq' else 'not_in' if c.operator=='ne' and field!='price' else c.operator
                    values.append((op,tuple(sorted(str(v).casefold() for v in c.values))))
                return sorted(values)
            unchanged={f for f in fields(proposed) if field_signature(base.groups[0],f)==field_signature(proposed.groups[0],f)}
            if 'price' in unchanged and any(k in current and current[k]!=getattr(old,k) for k in ('min_price','max_price')):
                raise ConstraintConflict('catalog_filter','本次价格提议仍是旧值，与用户明确的新预算不一致')
            # Repeating a saved field is not a new edit and needs no fabricated current quote.
            conditions=[c for c in proposed.groups[0].conditions if c.field not in unchanged]
            proposed=CatalogExpression(groups=[CatalogGroup(conditions=conditions)]) if conditions else CatalogExpression()
        if old is not None and update.operation=='replace' and not reset:
            # A planner's replace is only a field patch unless the user explicitly
            # resets the scope. Omitted old fields cannot disappear. New fields
            # still require raw-user cues; schema/provenance repair is bounded.
            from backend.tasks.constraint_updates import category_mentions,FIELD_CUES
            from backend.catalog.product_taxonomy import mentions
            cues={'category':bool(category_mentions(scope_text) or mentions(scope_text)),
                  'product_type':bool(mentions(scope_text)),
                  'color':bool('color' in current or 'color' in raw_clears or re.search(r'色|\bcolor|\bred\b|\bblack\b|\bblue\b|\bsilver\b',scope_text,re.I)),
                  'size':bool('size' in current or 'size' in raw_clears or re.search(r'尺寸|尺码|码|\bsize\b',scope_text,re.I)),
                  'price':bool(set(current)&{'min_price','max_price'} or set(raw_clears)&{'min_price','max_price'} or re.search(FIELD_CUES['max_price']+'|以内|以下|以上|低于|高于',scope_text,re.I))}
            if any(not cues[f] for f in fields(proposed)|(set(update.clear_fields)&fields(base))):
                raise ConstraintConflict('catalog_filter','替换提议包含用户没有提到的新字段或清除项',repairable=True)
            task.parameter_changes.append({'field':'catalog_filter','action':'replace_normalized_to_field_patch','source_index':update.source_index})
        amounts={Decimal(str(number(m[0]))) for m in re.finditer(NUMBER,update.source_text)}
        if any(c.field=='price' and Decimal(str(c.values[0])) not in amounts for g in proposed.groups for c in g.conditions):
            raise ConstraintConflict('catalog_filter','价格值在引用的用户原文中不存在；请修正原始输入索引和金额片段，相对价格必须从已展示商品核实',repairable=True)
        if operation=='replace':value=proposed
        elif operation=='clear':
            if not clears or proposed.groups:raise ConstraintConflict('catalog_filter','清除条件需明确字段，且不能同时增加条件')
            value=amend(base,None,clears)
        else:
            if fields(proposed)&clears:raise ConstraintConflict('catalog_filter','同一字段同时被要求清除和设置，请确认本次条件')
            value=amend(base,proposed,clears)
        task.parameter_sources['catalog_filter']={'source_index':update.source_index,'source_text':update.source_text,
            'operation':operation,'verified_by':'typed_query_contract','revision':task.revision}
        if reset:task.parameter_sources['catalog_filter']['scope_source_text']=scope_text
        if 'price' in fields(proposed)|clears or any(k in current for k in ('min_price','max_price')):
            for key in ('min_price','max_price'):task.parameter_sources[key]=dict(task.parameter_sources['catalog_filter'])
        task.parameter_changes.append({'field':'catalog_filter','action':operation,'source_index':update.source_index})
    elif old and old.catalog_filter is not None:
        value=base
        for key,v in legacy_changes.items():
            field={'product_category':'category','min_price':'price','max_price':'price'}.get(key,key)
            if field not in {'category','color','size','price'}:continue
            # Existing scalar adapters remain usable; normalize both price bounds together.
            p=task.parameters
            if field=='price':patch=effective(min_price=p.min_price,max_price=p.max_price)
            else:patch=effective(**{key:v})
            value=amend(value,patch,[field])
    else:return task
    if update is not None and item.parameters.catalog_filter is not None and item.parameters.catalog_filter!=value:
        raise ConstraintConflict('catalog_filter','重复提交的已存条件与本次修改不一致，请仅提交query_update')
    if impossible(value):raise ConstraintConflict('catalog_filter','本次包含、排除或价格条件互相矛盾，请确认要保留哪项条件')
    if update is not None:check_positive_scope(value,update.source_text)
    task.parameters.catalog_filter=value
    # Scalar compatibility mirrors, never a second competing interpretation.
    for key in ('product_category','color','size','min_price','max_price'):setattr(task.parameters,key,None)
    if len(value.groups)==1:
        for c in value.groups[0].conditions:
            if c.field=='price' and c.operator in {'gte','lte'}:setattr(task.parameters,{'gte':'min_price','lte':'max_price'}[c.operator],c.values[0])
            elif c.field in {'category','color','size'} and c.operator in {'eq','in'} and len(c.values)==1:setattr(task.parameters,{'category':'product_category'}.get(c.field,c.field),c.values[0])
    from backend.catalog.product_taxonomy import object_only_keyword
    if task.kind=='catalog_query' and object_only_keyword(task.parameters.keyword,value):task.parameters.keyword=None
    return task

def render_overview(data):
    names=capabilities()['categories'];rows=data['categories'];subject=data.get('catalog_subject','catalog')
    if rows:
        text='当前已分类样例目录'+('中的交通工具' if subject=='transport' else '')+'包括：\n'+'\n'.join('- '+names[r['category']]+'：'+str(r['count'])+'条商品记录。' for r in rows)
    else:text='当前已分类样例目录中，没有记录符合本次条件的'+('交通工具类别。' if subject=='transport' else '商品类别。')
    condition=describe_filters(data.get('catalog_filter'))
    if condition:text='本次目录条件：'+condition+'。\n'+text
    if data.get('unclassified_count'):text+='\n目录另有'+str(data['unclassified_count'])+'条未分类记录，无法仅凭现有分类确认它们是否符合本次需求。'
    return text+'\n以上为历史样例目录范围，不代表实时库存或在售状态。'

def describe_filters(value):
    names={'category':'类别','product_type':'商品类型','color':'颜色','size':'尺寸','price':'目录标价'}
    ops={'in':'属于','not_in':'不属于','eq':'为','ne':'不为','lt':'小于','lte':'不超过','gt':'大于','gte':'不低于'}
    labels=capabilities()['categories']
    type_labels=capabilities()['product_types']
    return ' 或 '.join('（'+'，且'.join(names[c.field]+ops[c.operator]+'／'.join((labels if c.field=='category' else type_labels).get(v,str(v)) if c.field in {'category','product_type'} else str(v) for v in c.values) for c in g.conditions)+'）' for g in expression(value).groups)
