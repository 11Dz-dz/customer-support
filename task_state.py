"""对话任务记录。业务参数与身份分离；旧检查点没有 task 时按新任务处理。"""
from typing import Literal
import re
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from adventureworks_service import AdventureWorksService
from catalog_filters import ProductCategory, category_matches

TaskKind = Literal['general', 'catalog_query', 'product_search', 'product_detail', 'quote',
                   'order_query', 'payment_query', 'invoice_query', 'refund_policy',
                   'refund_request', 'system_status', 'error_explanation', 'human_handoff', 'handoff_status']
Focus = Literal['order', 'dates', 'amounts', 'items', 'payment', 'invoice', 'refund',
                'catalog', 'products', 'quote', 'inventory', 'promotion']
AGENTS = {'catalog_query': 'sales', 'product_search': 'sales', 'product_detail': 'sales',
          'quote': 'sales', 'order_query': 'billing', 'payment_query': 'billing',
          'invoice_query': 'billing', 'refund_policy': 'billing', 'refund_request': 'billing',
          'system_status': 'tech_support', 'error_explanation': 'tech_support',
          'human_handoff': 'billing', 'handoff_status': 'billing'}


def explicit_product_count(text):
    """Normalize explicit presentation units, independently of prices/purchase quantities.

    This is a field normalizer, not an intent router or a whole-question matcher.
    Ambiguous/multiple counts are left to the planner rather than guessed.
    """
    numeral=r'[0-9]{1,3}|[一二两三四五六七八九十]{1,3}'
    patterns=[rf'(?<![0-9一二两三四五六七八九十])({numeral})\s*款',
              rf'(?:推荐|挑选|挑|选出|展示|列出|给出|找|搜索|检索)\s*(?:给我|一下|出|最多|至少|只要|仅)?\s*({numeral})\s*(?:种|个|件|条)']
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


class TaskParameters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    order_id: StrictInt | str | None = Field(default=None, description='订单数字编号或带SO前缀的订单号；程序统一校验并转成数字')
    product_ids: list[StrictInt] = Field(default_factory=list, max_length=20)
    product_position: StrictInt | None = Field(default=None, ge=1, le=10000,
        description='用户明确说第几件/第几个商品时的1起始位置；程序根据目标任务已验证的展示顺序解析，不能猜ID')
    quantity: StrictInt | None = Field(default=None, gt=0, le=10000)
    keyword: str | None = Field(default=None, max_length=200)
    product_category: ProductCategory | None = Field(default=None,
        description='语义商品搜索必填对象类别：bicycles整车、components零部件、clothing服装、accessories装备、all不限；不能因为描述提到车就把配件当整车')
    min_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    color: str | None = Field(default=None, max_length=200)
    size: str | None = Field(default=None, max_length=200)
    sort_by: Literal['price', 'name', 'product_id'] | None = None
    sort_order: Literal['asc', 'desc'] | None = None
    limit: StrictInt | None = Field(default=None, ge=1, le=20)
    operation: Literal['list', 'count', 'stats'] | None = None
    include_zero_price: bool | None = None
    quote_grouping: Literal['combined', 'separate'] | None = None
    component: Literal['database', 'server', 'api'] | None = None
    error_code: str | None = Field(default=None, max_length=100)
    # 明确清除旧条件；没有提供字段与删除字段是两种动作。
    clear_fields: list[Literal['keyword', 'min_price', 'max_price', 'color', 'size', 'product_position']] = Field(default_factory=list)

    @field_validator('order_id')
    @classmethod
    def normalize_order(cls, value):
        if value is None:
            return None
        number = AdventureWorksService._order_id(value)
        if number is None:
            raise ValueError('订单号格式或范围无效')
        return number

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
    @field_validator('order_id', mode='wrap')
    @classmethod
    def invalid_order_requires_clarification(cls, value, handler):
        try:
            return handler(value)
        except ValueError:
            # An invalid required identifier must never reach SQL or become an unfiltered query.
            return None


class PlannedTask(RouteDecision):
    parameters: PlannerParameters = Field(default_factory=PlannerParameters)

    @field_validator('parameters', mode='before')
    @classmethod
    def adapt_parameters(cls, value):
        return value.model_dump() if isinstance(value, TaskParameters) else value

    ref: str = Field(default='T1', pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,31}$')
    target_task_id: str | None = None
    query: str = Field(default='', max_length=2000,
                       description='仅包含本子任务的用户需求；不得加入用户没要求的操作')
    depends_on: list[str] = Field(default_factory=list, max_length=3)
    dependency_mode: Literal['none', 'order_products', 'selected_product'] = 'none'


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tasks: list[PlannedTask] = Field(default_factory=list, max_length=4)
    cancel_all: bool = False

    @model_validator(mode='after')
    def validate_references(self):
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
    evidence_call_ids: list[str] = Field(default_factory=list)
    displayed_product_ids: list[int] = Field(default_factory=list)
    failure_code: str | None = None
    operations: list[OperationRecord] = Field(default_factory=list, max_length=12)
    retry_failed: bool = False
    retry_operation_ids: list[str] = Field(default_factory=list, max_length=12)


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
        elif agent == 'billing':
            values = {'order_id': old.parameters.order_id}
    supplied = decision.parameters.model_dump(exclude_none=True)
    supplied = {k: v for k, v in supplied.items() if v != []}
    values.update(supplied)
    for key in decision.parameters.clear_fields:
        values[key] = None
    values['clear_fields'] = []
    params = TaskParameters.model_validate(values)
    task = ConversationTask(kind=kind, agent=agent, parameters=params, focus=decision.focus,
                            task_id=old.task_id if same and old.kind == kind else uuid4().hex,
                            revision=old.revision + 1 if same and old.kind == kind else 1)
    if decision.cancel:
        task.status = 'cancelled'
        return task
    required = {'order_query': ['order_id'], 'payment_query': ['order_id'], 'invoice_query': ['order_id'],
                'quote': ['product_ids', 'quantity'], 'product_detail': ['product_ids'],
                'system_status': ['component'], 'error_explanation': ['error_code']}.get(kind, [])
    task.awaiting_fields = [key for key in required if not getattr(params, key)]
    if task.awaiting_fields:
        task.status = 'awaiting_input'
    return task


def missing_fields(task):
    required = {'order_query': ['order_id'], 'payment_query': ['order_id'], 'invoice_query': ['order_id'],
                'quote': ['product_ids', 'quantity'], 'product_detail': ['product_ids'],
                'system_status': ['component'], 'error_explanation': ['error_code']}.get(task.kind, [])
    return [key for key in required if not getattr(task.parameters, key)]


def task_instruction(task):
    """只包含业务上下文；身份始终由 session_identity 提供。"""
    from context_budget import compact_task_record
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
        task.displayed_product_ids = valid_selection or ordered_products[:10 if task.agent == 'sales' else len(ordered_products)]
    if task.agent == 'sales' and not task.parameters.product_ids:
        selected = [pid for pid in selected_product_ids if pid in products]
        if len(selected) == 1:
            task.parameters.product_ids = selected
        elif len(products) == 1:
            task.parameters.product_ids = list(products)
    return task
