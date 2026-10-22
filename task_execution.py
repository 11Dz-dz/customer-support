"""已知只读任务直接执行；开放式检索和复杂任务仍由专家 ReAct 处理。"""
import json
from uuid import uuid4
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage
from tool_execution import guarded_tool_call
from task_operations import retry_operations
from tool_result import exception_result, normalize_tool_message


def clarification(task):
    labels = {'order_id': '有效的订单号', 'product_ids': '商品编号（多个候选时请明确选择）',
              'quantity': '购买数量', 'component': '要检查的组件：服务器、数据库或模型 API',
              'error_code': '错误代码'}
    if task.status == 'cancelled':
        return '已结束当前任务，本次没有提交业务操作。你可以提出新的问题。'
    return '请提供' + '、'.join(labels.get(key, key) for key in task.awaiting_fields) + '，再继续处理。'


def read_operations(task):
    """任务→操作映射，不匹配用户原句、不执行写操作。None 表示交给专家。"""
    p = task.parameters
    if task.kind == 'catalog_query':
        args = p.model_dump(include={'keyword', 'min_price', 'max_price', 'color', 'size',
                                     'sort_by', 'sort_order', 'limit', 'operation'}, exclude_none=True)
        if p.include_zero_price is not None:
            args['include_zero_price'] = p.include_zero_price
        return [('query_products', args)]
    if task.kind in {'order_query', 'payment_query', 'invoice_query'}:
        names = {'order_query': 'query_order', 'payment_query': 'check_payment_status',
                 'invoice_query': 'query_invoice'}
        selected = [names[task.kind]]
        if task.kind == 'order_query':
            selected += [name for field, name in [('payment', 'check_payment_status'), ('invoice', 'query_invoice')]
                         if field in task.focus]
        return [(name, {'order_id': str(p.order_id)}) for name in selected]
    if task.kind == 'product_detail':
        selected = [('get_product_info', {'product_name': str(pid)}) for pid in p.product_ids]
        if 'promotion' in task.focus:
            selected += [('check_promotion', {'product': str(pid)}) for pid in p.product_ids]
        return selected
    if task.kind == 'quote':
        groups = [[{'product_id': pid, 'quantity': p.quantity}] for pid in p.product_ids]
        if p.quote_grouping != 'separate':
            groups = [sum(groups, [])]
        return [('calculate_price', {'bundles': [{'items': items} for items in groups]})]
    if task.kind == 'system_status':
        return [('check_system_status', {'component': p.component})]
    if task.kind == 'error_explanation':
        return [('search_error_code', {'error_code': p.error_code})]
    return None


def execute_read_task(task, tools, config):
    operations=retry_operations(task) if task.retry_failed else read_operations(task)
    return execute_operations(task, operations, tools, config)


def seed_product_search(task, query, tools, config):
    """Get required evidence before the expert plans; empty model replies cannot skip retrieval."""
    p = task.parameters
    query = query.strip() or p.keyword or ''
    if p.keyword and p.keyword not in query:
        query = query + ' ' + p.keyword
    operations = [('search_products', {'query': query[:200],
                   'min_price': p.min_price, 'max_price': p.max_price, 'product_category': p.product_category,
                   'limit': min(max(p.limit or 5, 5), 10)})]
    return execute_operations(task, operations, tools, config, include_plan=False)


def execute_operations(task, operations, tools, config, *, include_plan=True):
    if operations is None:
        return None
    # 全部预检查后才执行，防止一半直接执行一半回退造成重复动作。
    if any(name not in tools for name, _ in operations) or len(operations) > 6:
        return None
    calls = [{'name': name, 'args': args, 'id': 'task-' + uuid4().hex} for name, args in operations]
    delta = [AIMessage(content='', tool_calls=calls)]
    for call in calls:
        request = SimpleNamespace(tool_call=call, tool=tools[call['name']], runtime=SimpleNamespace(config=config))
        try:
            value = guarded_tool_call(request, lambda req: req.tool.invoke({**req.tool_call, 'type': 'tool_call'}, config))
        except Exception as exc:
            # Record an individual failure and continue the other read-only operations.
            from chat_events import emit_event
            emit_event('operation_error',config,tool=call['name'],error_type=type(exc).__name__)
            value=ToolMessage(content=exception_result(exc).model_dump_json(),
                 name=call['name'],tool_call_id=call['id'],status='error')
        if not isinstance(value, ToolMessage):
            value = ToolMessage(content=json.dumps(value, ensure_ascii=False), name=call['name'], tool_call_id=call['id'])
        value=normalize_tool_message(value,call['name'])
        delta.append(value)
    if not include_plan:
        return delta
    focus = task.focus or {'quote': ['quote'], 'product_detail': ['products'],
                           'payment_query': ['payment'], 'invoice_query': ['invoice']}.get(task.kind, [])
    plan = {'kind': 'data', 'focus': focus, 'product_ids': task.parameters.product_ids,
            'order_id': task.parameters.order_id, 'explanations': []}
    delta.append(AIMessage(content=json.dumps(plan, ensure_ascii=False)))
    return delta
