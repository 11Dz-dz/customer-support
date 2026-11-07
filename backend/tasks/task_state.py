"""对话任务记录。业务参数与身份分离；旧检查点没有 task 时按新任务处理。"""
from typing import Literal
import re
from uuid import uuid4

from pydantic import PrivateAttr, BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema
from backend.orders.adventureworks_service import AdventureWorksService
from backend.catalog.catalog_filters import ProductCategory, category_matches
from backend.catalog.catalog_query import CatalogExpression,CatalogUpdate

TaskKind = Literal['conversation', 'general', 'catalog_query', 'product_search', 'product_detail', 'product_variants', 'quote',
                   'order_query', 'payment_query', 'invoice_query', 'refund_policy',
                   'refund_request', 'system_status', 'error_explanation', 'human_handoff', 'handoff_status']
Focus = Literal['order', 'dates', 'amounts', 'items', 'payment', 'invoice', 'refund',
                'catalog', 'products', 'quote', 'inventory', 'promotion']
AGENTS = {'conversation':'sales','catalog_query': 'sales', 'product_search': 'sales', 'product_detail': 'sales',
          'product_variants':'sales','quote': 'sales', 'order_query': 'billing', 'payment_query': 'billing',
          'invoice_query': 'billing', 'refund_policy': 'billing', 'refund_request': 'billing',
          'system_status': 'tech_support', 'error_explanation': 'tech_support',
          'human_handoff': 'billing', 'handoff_status': 'billing'}


def presentation_count_matches(text):
    """Quantity intent with exact offsets; an unknown count keeps value=None.

    These matches decide whether to request model repair. Actual field values
    still go through the existing task-specific quantity validator.
    """
    numeral = r'(?:[0-9]{1,3}|[一二两三四五六七八九十]{1,3}|几)'
    pattern = re.compile(
        rf'(?<![0-9一二两三四五六七八九十])'
        rf'(?:(?P<prefix>前|top\s*)(?P<rank>{numeral})(?![0-9])'
        rf'|(?P<count>{numeral})\s*(?P<unit>个|款|条|件|种|样|家|辆))', re.I)
    digits = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
              '六': 6, '七': 7, '八': 8, '九': 9}
    matches = []
    for match in pattern.finditer(text):
        suffix = text[match.end():].lstrip()
        if re.match(r'问题|意思|方面|事情|事|天|小时|分钟|年|月|轮子|道路', suffix):
            continue
        token = match.group('rank') or match.group('count')
        if token.isascii() and token.isdigit():
            value = int(token)
        elif token == '几':
            value = None
        elif '十' in token:
            left, right = token.split('十', 1)
            if left not in ('', *digits) or right not in ('', *digits):
                continue
            value = digits.get(left, 1) * 10 + digits.get(right, 0)
        else:
            value = digits.get(token)
            if value is None:
                continue
        matches.append({'span': [match.start(), match.end()], 'value': value,
                        'unit': match.group('unit') or match.group('prefix').strip().lower()})
    return matches


def explicit_product_count(text):
    """Normalize explicit presentation units, independently of prices/purchase quantities.

    This is a field normalizer, not an intent router or a whole-question matcher.
    Ambiguous/multiple counts are left to the planner rather than guessed.
    """
    numeral=r'[0-9]{1,3}|[一二两三四五六七八九十]{1,3}'
    patterns=[rf'(?<![0-9一二两三四五六七八九十])({numeral})\s*款',
              rf'(?:推荐|挑选|挑|选出|展示|列出|给出|给|返回|找|搜索|检索)\s*(?:给我|一下|出|最多|至少|只要|仅)?\s*({numeral})\s*(?:种|个|件|条|辆)']
    digits={'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    found=set()
    for pattern in patterns:
        for token in re.findall(pattern,text):
            if token.isascii() and token.isdigit():value=int(token)
            elif token=='十':value=10
            elif token.count('十')==1:
                left,right=token.split('十')
                if (left and left not in digits) or (right and right not in digits):continue
                value=digits.get(left,1)*10+digits.get(right,0)
            else:value=digits.get(token)
            if value is not None:found.add(value)
    return next(iter(found)) if len(found)==1 and 1<=next(iter(found))<=20 else None


def turn_user_inputs(messages, input_ids=()):
    """Only actual user messages from this turn; never use projected evidence or rewritten queries."""
    from langchain_core.messages import HumanMessage
    allowed=set(input_ids)
    rows=[m.content for m in messages if isinstance(m,HumanMessage) and
          (m.id in allowed if allowed else False) and isinstance(m.content,str)]
    if allowed:return rows
    return next(([m.content] for m in reversed(messages) if isinstance(m,HumanMessage) and isinstance(m.content,str)),[])


def authoritative_product_count(original_inputs, items, item):
    """Repair explicit units only when one recommendation scope is unambiguous.

    Latest explicit presentation count wins across supplement messages. Multiple
    product tasks remain scoped by the planner; never spread one task's count to another.
    """
    if item.task_type!='product_search' or item.cancel or item.retry_failed:return None
    scopes=[t for t in items if t.task_type in {'product_search','catalog_query','product_detail'} and not t.cancel]
    if len(scopes)!=1:return None
    for text in reversed(original_inputs):
        count=explicit_product_count(text)
        if count is not None:return count
        # A message with several presentation counts must not fall back to an
        # earlier value or apply a count across uncertain scopes.
        if re.search(r'(?:[0-9一二两三四五六七八九十]+)\s*款',text):return None
    return None


class QuoteLine(BaseModel):
    model_config=ConfigDict(extra='forbid')
    product_id: StrictInt=Field(gt=0,le=2147483647)
    quantity: StrictInt=Field(ge=1,le=10000)


class TaskParameters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    order_id: StrictInt | str | None = Field(default=None, description='订单数字编号或带SO前缀的订单号；程序统一校验并转成数字')
    order_scope: Literal['specific','mine'] = Field(default='specific',description='specific按编号查询；mine按已登录客户查找自己的订单候选，不接收客户ID')
    refund_filter: Literal['all','active','pending','approved','rejected','cancelled'] | None = Field(default=None,
        description='只用于mine列表；null全部订单，all有退款记录，active待审核/审核通过；其他值精确筛选申请状态')
    conversation_intent: Literal['greeting','thanks','goodbye','capabilities','out_of_scope'] | None = None
    scope_reason: Literal['creative','coding','general_knowledge','unrelated_service','other'] | None = None
    requested_item: str | None = Field(default=None, min_length=1, max_length=100)
    product_ids: list[StrictInt] = Field(default_factory=list, max_length=20)
    product_position: StrictInt | None = Field(default=None, ge=1, le=10000,
        description='用户明确说第几件/第几个商品时的1起始位置；程序根据目标任务已验证的展示顺序解析，不能猜ID')
    quantity: StrictInt | None = Field(default=None, gt=0, le=10000)
    quote_items: list[QuoteLine]=Field(default_factory=list,max_length=20,
        description='报价逐商品明细；不同SKU数量分别保存，单价和金额禁止由模型填写')
    keyword: str | None = Field(default=None, max_length=200)
    search_terms: list[str] = Field(default_factory=list,max_length=8,
        description='仅商品推荐：同一需求的简短中英文检索表达，最多8个；不含价格或礼貌词，不擅自增加整车/配件限制')
    include_variants: bool = Field(default=False,description='仅用户明确要求同型号不同颜色/尺寸对比时为true；默认推荐不同型号')
    variant_axis: Literal['color','size'] | None = Field(default=None,description='仅同款规格查询：查询其他颜色或尺寸，不重新推荐')
    variant_strict: bool = Field(default=False,description='仅用户明确要求保留价格范围或提供新预算时严格筛选，否则展示规格资料并说明价格关系')
    variant_anchor_id: StrictInt | None = Field(default=None,gt=0,le=2147483647,description='程序确认的规格查询参考SKU；不是模型可随意挑选的商品')
    variant_relation: Literal['lt','le','gt','ge'] | None = None
    product_category: ProductCategory | None = Field(default=None,
        description='语义商品搜索必填对象类别：bicycles整车、components零部件、clothing服装、accessories装备、all不限；不能因为描述提到车就把配件当整车')
    min_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    color: str | None = Field(default=None, max_length=200)
    size: str | None = Field(default=None, max_length=200)
    sort_by: Literal['price', 'name', 'product_id'] | None = None
    sort_order: Literal['asc', 'desc'] | None = None
    limit: StrictInt | None = Field(default=None, ge=1, le=20)
    operation: Literal['list', 'count', 'stats','overview','coverage'] | None = None
    catalog_filter: CatalogExpression | None = None
    catalog_subject: Literal['catalog','transport'] = 'catalog'
    include_zero_price: bool | None = None
    quote_grouping: Literal['combined', 'separate'] | None = None
    component: Literal['database', 'server', 'api'] | None = None
    error_code: str | None = Field(default=None, max_length=100)
    # 明确清除旧条件；没有提供字段与删除字段是两种动作。
    clear_fields: list[Literal['keyword', 'min_price', 'max_price', 'color', 'size', 'product_position']] = Field(default_factory=list)

    @field_validator('search_terms')
    @classmethod
    def validate_search_terms(cls,values):
        if any(not v.strip() or len(v)>80 for v in values):raise ValueError('检索表达必须为1–80字符')
        return list(dict.fromkeys(v.strip() for v in values))

    @field_validator('order_id')
    @classmethod
    def normalize_order(cls, value):
        if value is None:
            return None
        number = AdventureWorksService._order_id(value)
        if number is None:
            raise ValueError('订单号格式或范围无效')
        return number

    @field_validator('size',mode='before')
    @classmethod
    def normalize_size(cls,value):
        # Numeric catalog sizes are stored as strings, not quantities.
        return str(value) if type(value) is int else value

    @field_validator('color')
    @classmethod
    def normalize_color(cls, value):
        # 字段词汇规范化，适用于所有目录查询；不根据完整用户问句写分支。
        aliases = {'红': 'Red', '红色': 'Red', '紅色': 'Red', 'red': 'Red',
                   '黑': 'Black', '黑色': 'Black', 'black': 'Black',
                   '蓝': 'Blue', '蓝色': 'Blue', '藍色': 'Blue', 'blue': 'Blue',
                   '白': 'White', '白色': 'White', 'white': 'White',
                   '黄': 'Yellow', '黄色': 'Yellow', '黃色': 'Yellow', 'yellow': 'Yellow',
                   '银': 'Silver', '银色': 'Silver', '銀色': 'Silver', 'silver': 'Silver',
                   '灰': 'Grey', '灰色': 'Grey', 'grey': 'Grey', 'gray': 'Grey',
                   '多色': 'Multi', 'multi': 'Multi', '银黑': 'Silver/Black', 'silver/black': 'Silver/Black'}
        return aliases.get(value.strip().lower(), value.strip()) if value is not None else None


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    next: Literal['tech_support', 'sales', 'billing']
    task_type: TaskKind = 'general'
    continuation: bool = False
    cancel: bool = False
    retry_failed: bool = False
    parameters: TaskParameters = Field(default_factory=TaskParameters)
    focus: list[Focus] = Field(default_factory=list, max_length=13,
                             description='用户要确认的事实维度，必须保留：优惠/折扣=promotion，库存=inventory，报价=quote，订单金额=amounts，支付=payment，发票=invoice，日期=dates，商品介绍=products；完整详情可留空')


class PlannerParameters(TaskParameters):
    # Persisted conditions are context, not a writable planner field.
    catalog_filter: SkipJsonSchema[CatalogExpression | None] = None
    @field_validator('quantity',mode='wrap')
    @classmethod
    def invalid_quantity_requires_clarification(cls,value,handler):
        try:return handler(value)
        except ValueError:return None
    @model_validator(mode='before')
    @classmethod
    def invalid_order_does_not_broaden_scope(cls,value):
        if isinstance(value,dict) and value.get('order_id') is not None and AdventureWorksService._order_id(value['order_id']) is None:
            return {**value,'order_scope':'specific'}
        return value

    @field_validator('order_id', mode='wrap')
    @classmethod
    def invalid_order_requires_clarification(cls, value, handler):
        try:
            return handler(value)
        except ValueError:
            # An invalid required identifier must never reach SQL or become an unfiltered query.
            return None


class FieldUpdate(BaseModel):
    """A proposed customer constraint change, grounded in one original message."""
    model_config = ConfigDict(extra='forbid')
    field: Literal['product_category','min_price','max_price','color','size','limit',
                   'quantity','include_variants','include_zero_price','sort_by','sort_order',
                   'operation','quote_grouping']
    operation: Literal['set','clear'] = 'set'
    value: str | int | float | bool | None = None
    source_index: int = Field(ge=0, description='本轮原始用户输入数组的0起始索引')
    source_text: str = Field(min_length=1, max_length=500, description='该条用户原文的连续片段，包含值、单位和否定/取消等修饰，不能引用模型改写')


class PlannedTask(RouteDecision):
    parameters: PlannerParameters = Field(default_factory=PlannerParameters)
    query_update: CatalogUpdate | None = Field(default=None,description='商品条件统一补丁：包含/排除/组合条件及用户原文依据；不要将否定对象写入旧标量字段')

    @field_validator('parameters', mode='before')
    @classmethod
    def adapt_parameters(cls, value):
        return value.model_dump(exclude_unset=True) if isinstance(value, TaskParameters) else value

    field_updates: list[FieldUpdate] = Field(default_factory=list,max_length=30)
    ref: str = Field(default='T1', pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,31}$')
    source_index: int | None = Field(default=None, ge=0)
    source_text: str | None = Field(default=None, min_length=1, max_length=2000)
    target_task_id: str | None = None
    query: str = Field(default='', max_length=2000,
                       description='仅包含本子任务的用户需求；不得加入用户没要求的操作')
    depends_on: list[str] = Field(default_factory=list, max_length=3)
    dependency_mode: Literal['none', 'order_products', 'selected_product'] = 'none'


class TaskPlan(BaseModel):
    _refusal_before_rebuild: list = PrivateAttr(default_factory=list)
    model_config = ConfigDict(extra='forbid')
    tasks: list[PlannedTask] = Field(default_factory=list, max_length=4)
    cancel_all: bool = False
    clarification: str | None = Field(default=None,max_length=500,description="仅需求归属或条件有歧义时填写面向用户的问题；仍须给出任务候选，不能猜测执行。已知任务缺必填字段由awaiting_input处理")

    @model_validator(mode='after')
    def validate_references(self):
        refused=[]
        for index,item in enumerate(self.tasks):
            if item.task_type=='conversation' and item.parameters.conversation_intent=='out_of_scope':
                # A refusal is a fresh, inert reply. Planner fields cannot turn
                # it into cancellation, inheritance, retry or a dependency.
                self._refusal_before_rebuild.append(item.model_dump(mode='json'))
                self.tasks[index]=PlannedTask(next='sales',task_type='conversation',ref=item.ref,query=item.query,
                    source_index=item.source_index,source_text=item.source_text,
                    parameters=PlannerParameters(conversation_intent='out_of_scope',scope_reason=item.parameters.scope_reason))
                refused.append(item.ref)
        if refused and len(refused)==len(self.tasks):self.cancel_all=False
        if any(set(t.depends_on)&set(refused) for t in self.tasks):
            raise ValueError('Business tasks cannot depend on an out-of-scope reply')
        if not self.tasks and not self.cancel_all:
            raise ValueError('at least one task is required')
        refs = [t.ref for t in self.tasks]
        if len(refs) != len(set(refs)):
            raise ValueError('duplicate task references')
        # References may also identify existing persisted tasks; resolved by the scheduler.
        if any(t.ref in t.depends_on for t in self.tasks):
            raise ValueError('self dependency')
        return self


class OperationRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: str
    tool: str
    arguments: dict
    status: Literal['succeeded', 'failed', 'denied']
    call_id: str
    attempts: int = Field(default=1, ge=1)
    retryable: bool = False
    error_code: str | None = None
    outcome: str | None = None
    requires_review: bool = False


class ConversationTask(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Literal[1] = 1
    task_id: str = Field(default_factory=lambda: uuid4().hex)
    kind: TaskKind = 'general'
    agent: Literal['tech_support', 'sales', 'billing'] = 'sales'
    status: Literal['ready', 'awaiting_input', 'awaiting_dependency', 'completed', 'partial', 'empty', 'failed', 'denied', 'cancelled'] = 'ready'
    parameters: TaskParameters = Field(default_factory=TaskParameters)
    focus: list[Focus] = Field(default_factory=list)
    awaiting_fields: list[str] = Field(default_factory=list)
    revision: int = 1
    candidate_product_ids: list[int] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    dependency_mode: Literal['none', 'order_products', 'selected_product'] = 'none'
    query: str = ''
    parameter_sources: dict[str, dict] = Field(default_factory=dict)
    parameter_changes: list[dict] = Field(default_factory=list, max_length=30)
    evidence_call_ids: list[str] = Field(default_factory=list)
    displayed_product_ids: list[int] = Field(default_factory=list)
    failure_code: str | None = None
    operations: list[OperationRecord] = Field(default_factory=list, max_length=12)
    retry_failed: bool = False
    retry_operation_ids: list[str] = Field(default_factory=list, max_length=12)


def product_handoff_fields(source_kind,target_kind):
    product_kinds={'product_search','catalog_query','product_detail','product_variants'}
    return {'min_price','max_price','catalog_filter','product_category','color','size'} if source_kind in product_kinds and target_kind in product_kinds else set()


def prepare_task(decision, previous=None):
    """路由只提出任务，程序确定所属专家、参数继承范围和缺少的字段。"""
    if not isinstance(decision, RouteDecision):
        # 兼容旧测试路由及旧式适配器，不要求其具备新字段。
        decision = RouteDecision(next=decision.next)
    old = ConversationTask.model_validate(previous) if previous else None
    kind = decision.task_type
    if decision.continuation and old and kind == 'general' and old.kind!='refund_request':
        kind = old.kind
    agent = AGENTS.get(kind, decision.next)
    same = bool(decision.continuation and old and old.status != 'cancelled' and old.agent == agent)
    values = {}
    if same:
        # 同类任务保留条件；跨任务只保留可明确交接的对象，避免把报价数量带入订单。
        if old.kind == kind:
            values = old.parameters.model_dump()
        elif agent == 'sales':
            values = {'product_ids': old.parameters.product_ids}
            # Explicitly targeted product follow-ups share catalog constraints;
            # purchase quantities never leak into a different task kind.
            values.update(old.parameters.model_dump(include=product_handoff_fields(old.kind,kind)))
        elif agent == 'billing':
            values = {'order_id': old.parameters.order_id}
    supplied = decision.parameters.model_dump(exclude_none=True, exclude_unset=True)
    supplied = {k: v for k, v in supplied.items() if v != []}
    values.update(supplied)
    if same and old and decision.parameters.keyword is not None and decision.parameters.keyword!=old.parameters.keyword:
        values['search_terms']=decision.parameters.search_terms
    if decision.parameters.order_scope=='mine' and decision.parameters.order_id is None:
        values['order_id']=None
        values['refund_filter']=decision.parameters.refund_filter
    for key in decision.parameters.clear_fields:
        values[key] = None
    values['clear_fields'] = []
    params = TaskParameters.model_validate(values)
    if params.order_id is not None:params.order_scope='specific'
    task = ConversationTask(kind=kind, agent=agent, parameters=params, focus=decision.focus,
                            task_id=old.task_id if same and old.kind == kind else uuid4().hex,
                            revision=old.revision + 1 if same and old.kind == kind else 1,
                            parameter_sources=dict(old.parameter_sources) if same and old.kind==kind else {})
    if decision.cancel:
        task.status = 'cancelled'
        return task
    task.awaiting_fields = missing_fields(task)
    if task.awaiting_fields:
        task.status = 'awaiting_input'
    return task


def missing_fields(task):
    if task.kind=='order_query' and task.parameters.order_scope=='mine':return []
    if task.kind=='quote' and task.parameters.quote_items:
        known={line.product_id for line in task.parameters.quote_items}
        return ['quantity'] if set(task.parameters.product_ids)-known else []
    required = {'order_query': ['order_id'], 'payment_query': ['order_id'], 'invoice_query': ['order_id'],
                'quote': ['product_ids', 'quantity'], 'product_detail': ['product_ids'], 'product_variants':['variant_anchor_id','variant_axis'],
                'system_status': ['component'], 'error_explanation': ['error_code']}.get(task.kind, [])
    return [key for key in required if not getattr(task.parameters, key)]


def task_instruction(task):
    """只包含业务上下文；身份始终由 session_identity 提供。"""
    from backend.agent.context_budget import compact_task_record
    import json
    return ('当前结构化任务（对象仍需经工具验证，不代表权限或业务事实）：' + json.dumps(compact_task_record(task),ensure_ascii=False)
            + '。只处理当前任务；缺少必要信息先询问。达到目标后结束，不重复确认同一事实。')


def complete_task(task, result, selected_product_ids=()):
    """只从成功工具结果保留商品上下文；多个候选不自动等于用户已选定。"""
    task = task.model_copy(deep=True)
    task.status = result.status
    task.operations = result.operations
    task.retry_operation_ids = []  # Selection applies to one execution; remaining failures stay eligible.
    evidence = result.retained + result.current
    if evidence:
        task.evidence_call_ids = list(dict.fromkeys(row.call_id for row in evidence if row.call_id))
    products, ordered_products = set(), []
    for row in evidence:
        value = row.data
        if value.get('success') is not True:
            continue
        records = list(value.get('products', [])) + list(value.get('items', []))
        if isinstance(value.get('product'), dict):
            records.append(value['product'])
        if type(value.get('product_id')) is int:
            records.append(value)
        for bundle in value.get('bundles', []):
            records.extend(bundle.get('items', []))
        for p in records:
            if task.kind=='product_search' and not category_matches(p,task.parameters.product_category):
                continue
            pid = p.get('product_id')
            if type(pid) is int:
                if task.agent == 'billing' or pid not in products:
                    ordered_products.append(pid)
                products.add(pid)
    if evidence:
        task.candidate_product_ids = sorted(products)
        valid_selection = list(selected_product_ids) if all(pid in products for pid in selected_product_ids) else []
        task.displayed_product_ids = (valid_selection if task.kind=='product_search' else
                                      valid_selection or ordered_products[:10 if task.agent == 'sales' else len(ordered_products)])
    if task.kind=='product_search':
        task.parameters.product_ids=list(selected_product_ids) if len(selected_product_ids)==1 and selected_product_ids[0] in products else []
    elif task.kind=='product_variants':
        task.displayed_product_ids=list(selected_product_ids)
        task.parameters.product_ids=list(selected_product_ids) if len(selected_product_ids)==1 else []
    elif task.agent == 'sales' and not task.parameters.product_ids:
        selected = [pid for pid in selected_product_ids if pid in products]
        if len(selected) == 1:
            task.parameters.product_ids = selected
        elif len(products) == 1 and task.kind!='product_search':
            task.parameters.product_ids = list(products)
    return task
