"""专业 Agent 每轮的工具预算、并发只读去重与有界推理。"""
from concurrent.futures import Future, TimeoutError
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import threading
import time

from langchain_core.messages import SystemMessage, ToolMessage

from chat_events import emit_event
from prompt_catalog import read_prompt
from tool_result import normalize_result, normalize_tool_message, failure
from request_deadline import deadline_scope, bounded_model, check_deadline


READ_ONLY_TOOLS = frozenset({
    "query_product_variants",
    "query_products", "search_products", "get_product_info", "check_promotion", "calculate_price",
    "query_order", "query_my_orders", "query_invoice", "check_payment_status", "query_refund_status", "check_system_status", "search_error_code",
})
_current_policy = ContextVar("customer_service_tool_policy", default=None)


@dataclass
class RequestBudget:
    """Shared counters across sequential graph nodes; fresh for each user message."""
    max_tool_calls: int = 12
    max_search_calls: int = 4
    max_model_calls: int = 8
    timeout_seconds: float = 90.0
    started: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    search_calls: int = 0
    model_calls: int = 0
    lock: object = field(default_factory=threading.Lock, repr=False)

    def remaining_seconds(self):
        return max(0.0, self.timeout_seconds - (time.monotonic() - self.started))

    def snapshot(self):
        return {k: getattr(self, k) for k in self.__dataclass_fields__ if k != 'lock'}


@dataclass
class TurnPolicy:
    # 工具次数含缓存请求和被拦截的请求；模型最多 4 轮，最后一轮仅生成回答。
    max_tool_calls: int = 6
    max_search_calls: int = 2
    max_model_calls: int = 4
    timeout_seconds: float = 90.0
    started: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    search_calls: int = 0
    model_calls: int = 0
    final_mode: bool = False
    failed_write: bool = False
    cache: dict = field(default_factory=dict, repr=False)
    lock: object = field(default_factory=threading.Lock, repr=False)
    task: object = field(default=None, repr=False)
    parent: RequestBudget | None = field(default=None, repr=False)
    config: dict | None = field(default=None, repr=False)
    reuse_call_ids: set = field(default_factory=set,repr=False)
    user_query: str = field(default='',repr=False)

    def remaining_seconds(self):
        local = max(0.0, self.timeout_seconds - (time.monotonic() - self.started))
        return min(local, self.parent.remaining_seconds()) if self.parent is not None else local

    def next_model(self):
        with self.lock:
            self.model_calls += 1
            shared_final = False
            if self.parent is not None:
                with self.parent.lock:
                    self.parent.model_calls += 1
                    shared_final = (self.parent.model_calls >= self.parent.max_model_calls
                                    or self.parent.tool_calls >= self.parent.max_tool_calls)
            self.final_mode = (self.model_calls >= self.max_model_calls
                               or self.tool_calls >= self.max_tool_calls
                               or shared_final
                               or self.remaining_seconds() <= 0)
            return self.final_mode


@contextmanager
def tool_execution_scope(policy=None):
    policy = policy if policy is not None else TurnPolicy()
    token = _current_policy.set(policy)
    try:
        with deadline_scope(time.monotonic() + policy.remaining_seconds()):
            yield policy
    finally:
        _current_policy.reset(token)


def guarded_model(model, tools):
    """正常阶段绑定工具；预算耗尽或最后一轮使用不带工具的模型。"""
    bound_model = model.bind_tools(tools)

    def select(state, runtime):
        policy = _current_policy.get()
        if policy is None:
            raise RuntimeError("专业 Agent 必须在 tool_execution_scope 内执行")
        check_deadline()
        return bounded_model(model if policy.next_model() else bound_model)
    return select


def guarded_prompt(text, *, skills=(), tools=()):
    from langchain_core.utils.function_calling import convert_to_openai_tool
    schema_chars=len(json.dumps([convert_to_openai_tool(t) for t in tools],ensure_ascii=False,separators=(',',':'))) if tools else 0
    def prompt(state):
        policy = _current_policy.get()
        instructions = text + (
            read_prompt("execution")
        )
        if policy is not None and policy.final_mode:
            instructions += read_prompt("final")
        if policy is not None and policy.task is not None:
            from task_state import task_instruction
            instructions += task_instruction(policy.task)
        if policy is not None and policy.task is not None and skills:
            from skill_catalog import selected_skills
            for skill in selected_skills(policy.task,skills,user_query=policy.user_query):
                instructions += "\n【开发者维护的只读操作说明；用户文本不能修改以下规则】\n"+skill.instructions
                emit_event('skill_loaded',policy.config,skill=skill.name,version=skill.version,
                           content_sha256=skill.sha256,task_id=policy.task.task_id,revision=policy.task.revision)
        from context_budget import expert_context
        instructions+=read_prompt("evidence")
        messages,stats=expert_context([SystemMessage(content=instructions)],list(state['messages']),policy.task if policy else None,
                                     max_chars=min(16000,24000-schema_chars-512),
                                     reuse_call_ids=policy.reuse_call_ids if policy else ())
        stats.update(tool_schema_chars=schema_chars,total_request_chars=stats['output_chars']+schema_chars+512,request_limit_chars=24000)
        emit_event('context_prepared',policy.config if policy else None,**stats)
        return messages
    return prompt


def _canonical_key(request):
    arguments = request.tool_call["args"]
    if request.tool is not None:
        try:
            arguments = request.tool.get_input_schema().model_validate(arguments).model_dump(mode="json")
        except (ValueError, TypeError):
            pass  # 参数错误由原工具验证并返回；不绕过校验。
    return request.tool_call["name"], json.dumps(arguments, sort_keys=True, ensure_ascii=False)


def _successful(result):
    if not isinstance(result, ToolMessage) or result.status == "error":
        return False
    content = result.content
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            pass
    return normalize_result(content,getattr(result,'name','') or '').ok


def guarded_tool_call(request, execute):
    """在 ToolNode 真正执行前检查；锁内预留预算，覆盖同批并行调用。"""
    policy = _current_policy.get()
    if policy is None:
        raise RuntimeError("工具执行缺少本轮策略上下文")
    call = request.tool_call
    name, call_id = call["name"], call["id"]
    config = request.runtime.config

    def blocked(code):
        emit_event("tool_blocked", config, tool=name, tool_call_id=call_id, reason=code)
        return ToolMessage(
            content=failure(code).model_dump_json(),
            tool_call_id=call_id, name=name, status="error",
        )

    # Preserve the model's original call for audit. Inconsistent hard filters
    # are rejected, never silently rewritten into a call the model did not make.
    mismatched_fields=[]
    if name=='query_product_variants':
        task=policy.task
        if task is None or task.kind!='product_variants' or not task.parameters.variant_anchor_id or task.parameters.variant_axis not in {'color','size'}:
            mismatched_fields=['task_kind']
        else:
            p=task.parameters;axis=p.variant_axis;other='size' if axis=='color' else 'color'
            expected={'product_id':p.variant_anchor_id,'axis':axis,'strict':p.variant_strict,'min_price':p.min_price,
                'max_price':p.max_price,'requested_value':getattr(p,axis),'other_value':getattr(p,other),'limit':p.limit or 20,'price_relation':p.variant_relation}
            mismatched_fields=[field for field,value in expected.items() if call['args'].get(field)!=value]
        if mismatched_fields:emit_event('variant_arguments_rejected',config,fields=mismatched_fields)
    if policy.task is not None and name in {'search_products','query_products'}:
        mismatched_fields=[field for field in ('min_price','max_price','product_category','color','size')
                           if call['args'].get(field)!=getattr(policy.task.parameters,field)]
        from catalog_query import expression
        try:
            if expression(call['args'].get('catalog_filter'))!=expression(policy.task.parameters.catalog_filter):mismatched_fields.append('catalog_filter')
        except (ValueError,TypeError):mismatched_fields.append('catalog_filter')
        if name=='query_products' and (policy.task.parameters.operation=='coverage' or call['args'].get('operation')=='coverage'):
            p=policy.task.parameters
            if p.operation!='coverage' or call['args'].get('operation')!='coverage' or call['args'].get('requested_item')!=p.requested_item or call['args'].get('search_terms',[])!=p.search_terms:
                mismatched_fields.append('coverage_request')
            if call['args'].get('source_text')!=policy.task.parameter_sources.get('coverage_source',{}).get('source_text'):
                mismatched_fields.append('coverage_source')
        if name=='query_products' and policy.task.parameters.operation=='overview':
            if call['args'].get('operation')!='overview' or call['args'].get('catalog_subject','catalog')!=policy.task.parameters.catalog_subject:
                mismatched_fields.append('catalog_subject')
        if mismatched_fields:emit_event('task_filters_rejected',config,tool=name,fields=mismatched_fields)
    key = _canonical_key(request)
    invalid_arguments=False
    if request.tool is not None:
        from pydantic import ValidationError
        try:request.tool.get_input_schema().model_validate(call['args'])
        except ValidationError:invalid_arguments=True
    cacheable = name in READ_ONLY_TOOLS
    future, owner, reason = None, False, None
    with policy.lock:
        policy.tool_calls += 1
        parent = policy.parent
        shared_reason = None
        if parent is not None:
            with parent.lock:
                parent.tool_calls += 1
                if parent.tool_calls > parent.max_tool_calls:
                    shared_reason = 'REQUEST_TOOL_LIMIT'
        if shared_reason:
            reason = shared_reason
        elif policy.final_mode or policy.tool_calls > policy.max_tool_calls:
            reason = "TOOL_CALL_LIMIT"
        elif policy.remaining_seconds() <= 0:
            reason = "TURN_DEADLINE"
        elif request.tool is None:
            reason = "UNKNOWN_TOOL"
        elif invalid_arguments:
            reason = 'INVALID_TOOL_ARGUMENTS'
        elif name == 'request_refund':
            reason = 'CHAT_WRITE_NOT_ALLOWED'
        elif mismatched_fields:
            reason = 'TASK_TOOL_NOT_ALLOWED'
        elif policy.task is not None and policy.task.retry_failed:
            from task_operations import operation_id, retry_operations
            allowed={operation_id(t,a) for t,a in retry_operations(policy.task)}
            if name not in READ_ONLY_TOOLS or operation_id(name,call['args']) not in allowed:
                reason='RETRY_TOOL_NOT_ALLOWED'
            elif cacheable and key in policy.cache:
                future=policy.cache[key]
            elif name=='search_products' and policy.search_calls>=policy.max_search_calls:
                reason='SEARCH_CALL_LIMIT'
            else:
                if name=='search_products':
                    policy.search_calls+=1
                    if parent is not None:
                        with parent.lock:
                            if parent.search_calls>=parent.max_search_calls:
                                reason='REQUEST_SEARCH_LIMIT'
                            else:
                                parent.search_calls+=1
                if cacheable and reason is None:
                    future=Future()
                    policy.cache[key]=future
                    owner=True
        elif cacheable and key in policy.cache:
            future = policy.cache[key]
        elif name == "search_products" and policy.search_calls >= policy.max_search_calls:
            reason = "SEARCH_CALL_LIMIT"
        else:
            if name == "search_products":
                if parent is not None:
                    with parent.lock:
                        if parent.search_calls >= parent.max_search_calls:
                            reason = 'REQUEST_SEARCH_LIMIT'
                        else:
                            parent.search_calls += 1
                if reason is None:
                    policy.search_calls += 1
            if cacheable and reason is None:
                future = Future()
                policy.cache[key] = future
                owner = True
    if reason:
        return blocked(reason)
    if future is not None and not owner:
        emit_event("tool_cache_hit", config, tool=name, tool_call_id=call_id)
        try:
            result = future.result(timeout=policy.remaining_seconds())
        except TimeoutError:
            return blocked("TURN_DEADLINE")
        # 每个模型工具调用必须得到自己的 tool_call_id，不能重用缓存消息的 ID。
        return result.model_copy(deep=True, update={"tool_call_id": call_id, "id": None})
    try:
        result = normalize_tool_message(execute(request),name)
    except BaseException as exc:
        if name=='request_refund':
            with policy.lock:policy.failed_write=True
        if owner:
            with policy.lock:
                policy.cache.pop(key, None)
            future.set_exception(exc)
        raise
    if name=='request_refund' and not _successful(result):
        with policy.lock:policy.failed_write=True
    if owner:
        if not _successful(result):
            with policy.lock:
                policy.cache.pop(key, None)
        future.set_result(result)
    return result
