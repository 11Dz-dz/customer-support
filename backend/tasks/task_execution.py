"""已知只读任务直接执行；开放式检索和复杂任务仍由专家 ReAct 处理。"""
import json
from uuid import uuid4
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage
from backend.agent.tool_execution import guarded_tool_call
from backend.tasks.task_operations import retry_operations
from backend.agent.tool_result import exception_result, normalize_tool_message


def clarification(task):
    labels = {'order_id': '有效的订单号', 'product_ids': '商品编号（多个候选时请明确选择）',
              'quantity': '购买数量', 'component': '要检查的组件：服务器、数据库或模型 API',
              'error_code': '错误代码'}
    labels['variant_axis']='要查询的规格：颜色或尺寸'
    labels['variant_anchor_id']='商品编号（或先查询商品）'
    if task.status == 'cancelled':
        return '已结束当前任务，本次没有提交业务操作。你可以提出新的问题。'
    if task.kind=='quote' and 'quantity' in task.awaiting_fields:
        known={line.product_id:line.quantity for line in task.parameters.quote_items}
        pending=[pid for pid in task.parameters.product_ids if pid not in known]
        if pending:
            saved='已保留：'+'；'.join('商品'+str(pid)+'，数量'+str(qty) for pid,qty in known.items())+'。\n' if known else ''
            return saved+'请提供'+('、'.join('商品'+str(pid) for pid in pending))+'的购买数量，再继续核算；本次尚未形成完整报价。'
    return '请提供' + '、'.join(labels.get(key, key) for key in task.awaiting_fields) + '，再继续处理。'


def read_operations(task):
    """任务→操作映射，不匹配用户原句、不执行写操作。None 表示交给专家。"""
    p = task.parameters
    if task.kind=='product_variants':
        axis=p.variant_axis;other='size' if axis=='color' else 'color'
        return [('query_product_variants',{'product_id':p.variant_anchor_id,'axis':axis,'strict':p.variant_strict,
            'min_price':p.min_price,'max_price':p.max_price,'requested_value':getattr(p,axis),
            'other_value':getattr(p,other),'limit':p.limit or 20,'price_relation':p.variant_relation})]
    if task.kind=='order_query' and p.order_scope=='mine' and p.order_id is None:
        return [('query_my_orders',{'refund_filter':p.refund_filter,'limit':p.limit or 5})]
    if task.kind == 'catalog_query':
        if p.operation == 'coverage':
            return [('query_products', {'operation':'coverage','requested_item':p.requested_item,'search_terms':p.search_terms,'source_text':task.parameter_sources.get('coverage_source',{}).get('source_text')})]
        args = p.model_dump(include={'keyword', 'min_price', 'max_price', 'color', 'size',
                                     'sort_by', 'sort_order', 'limit', 'operation','product_category'}, exclude_none=True)
        if p.catalog_filter is not None:args['catalog_filter']=p.catalog_filter.model_dump(mode='json')
        if p.operation=='overview':
            args['catalog_subject']=p.catalog_subject
            args.pop('keyword',None)
        if p.include_zero_price is not None:
            args['include_zero_price'] = p.include_zero_price
        return [('query_products', args)]
    if task.kind in {'order_query', 'payment_query', 'invoice_query'}:
        names = {'order_query': 'query_order', 'payment_query': 'check_payment_status',
                 'invoice_query': 'query_invoice'}
        selected = [names[task.kind]]
        if task.kind == 'order_query' and task.focus == ['refund']:
            selected = ['query_refund_status']
        if task.kind == 'order_query':
            if 'refund' in task.focus and 'query_refund_status' not in selected:
                selected.append('query_refund_status')
            selected += [name for field, name in [('payment', 'check_payment_status'), ('invoice', 'query_invoice')]
                         if field in task.focus]
        return [(name, {'order_id': str(p.order_id)}) for name in selected]
    if task.kind == 'product_detail':
        selected = [('get_product_info', {'product_name': str(pid)}) for pid in p.product_ids]
        if 'promotion' in task.focus:
            selected += [('check_promotion', {'product': str(pid)}) for pid in p.product_ids]
        return selected
    if task.kind == 'quote':
        groups = [[line.model_dump()] for line in p.quote_items] if p.quote_items else [[{'product_id': pid, 'quantity': p.quantity}] for pid in p.product_ids]
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
    query = p.keyword or query.strip()
    operations = [('search_products', {'query': query[:200],
                   'min_price': p.min_price, 'max_price': p.max_price, 'product_category': p.product_category,
                   'keyword_terms':p.search_terms or [query[:80]],
                   'limit': min(max((p.limit or 5)*3,20),30)})]
    for field in ('color','size'):
        if getattr(p,field) is not None:operations[0][1][field]=getattr(p,field)
    if p.catalog_filter is not None:operations[0][1]['catalog_filter']=p.catalog_filter.model_dump(mode='json')
    return execute_operations(task, operations, tools, config, include_plan=False)


def execute_operations(task, operations, tools, config, *, include_plan=True):
    if operations is None:
        return None
    # 全部预检查后才执行，防止一半直接执行一半回退造成重复动作。
    if any(name not in tools for name, _ in operations) or len(operations) > 6:
        return None
    calls = [{'name': name, 'args': args, 'id': 'task-' + uuid4().hex} for name, args in operations]
    # Audit/evidence record only. The model input must not claim the model made this call.
    delta = [AIMessage(content='', tool_calls=calls, additional_kwargs={'program_execution': True})]
    for call in calls:
        request = SimpleNamespace(tool_call=call, tool=tools[call['name']], runtime=SimpleNamespace(config=config))
        try:
            value = guarded_tool_call(request, lambda req: req.tool.invoke({**req.tool_call, 'type': 'tool_call'}, config))
        except Exception as exc:
            # Record an individual failure and continue the other read-only operations.
            from backend.observability.chat_events import emit_event
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
