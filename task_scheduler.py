"""Bounded deterministic scheduling. The model proposes tasks; this module owns transitions."""
from langchain_core.messages import AIMessage, ToolMessage

from task_state import (ConversationTask, PlannedTask, RouteDecision, TaskPlan,
                        missing_fields, prepare_task)
from task_result import collect_evidence
from adventureworks_service import AdventureWorksService
from task_operations import retry_operations

TERMINAL = {'completed', 'empty', 'failed', 'denied', 'cancelled'}
MAX_SAVED_TASKS = 12
MAX_OPEN_TASKS = 8


class PlanConflict(ValueError):
    pass


def saved_tasks(state):
    rows = state.get('tasks') or ([state['task']] if state.get('task') else [])
    return [ConversationTask.model_validate(row) for row in rows]


def _target(item, old):
    if item.target_task_id:
        matches = [t for t in old if t.task_id == item.target_task_id]
        if len(matches) != 1:
            raise PlanConflict('找不到要继续的任务，请明确需要处理哪件事。')
        return matches[0]
    if not (item.continuation or item.cancel or item.retry_failed):
        return None
    if item.retry_failed:
        matches=[t for t in old if t.status in {'partial','failed'} and retry_operations(t)]
        if item.parameters.order_id is not None:
            matches=[t for t in matches if t.parameters.order_id==item.parameters.order_id]
    else:
        matches = [t for t in old if t.status != 'cancelled'
                   and (item.task_type == 'general' or t.kind == item.task_type)]
    waiting = [t for t in matches if t.status in {'awaiting_input', 'awaiting_dependency', 'ready','partial'}]
    if waiting:
        matches = waiting
    if len(matches) > 1:
        raise PlanConflict('有多个可能对应的任务，请说明要继续或取消哪一项（可提供任务编号）。')
    if matches:
        return matches[0]
    # A single completed task can supply an explicitly requested cross-kind follow-up.
    if not item.retry_failed and item.continuation and len(old) == 1 and old[0].status != 'cancelled':
        return old[0]
    return None


def prepare_plan(decision, state, query):
    old = saved_tasks(state)
    if isinstance(decision, RouteDecision) and not isinstance(decision, PlannedTask):
        decision = TaskPlan(tasks=[PlannedTask(**decision.model_dump(), query=query)])
    if not isinstance(decision, TaskPlan):
        # Compatibility for existing adapters/tests that return an agent-only decision.
        decision = TaskPlan(tasks=[PlannedTask(next=decision.next, query=query)])
    # A bare identifier does not tell us which of several waiting order tasks it belongs to,
    # even if the model happens to choose a target. Enforce ambiguity outside the prompt.
    bare_order = AdventureWorksService._order_id(query.strip())
    waiting_orders = [t for t in old if t.status == 'awaiting_input' and 'order_id' in t.awaiting_fields]
    if bare_order is not None and len(waiting_orders) > 1:
        raise PlanConflict('有多项任务都在等待订单号，请说明这个编号对应哪一项（可说第一项、第二项或任务编号）。')
    cancelled_ids = []
    if decision.cancel_all:
        for i, t in enumerate(old):
            if t.status not in TERMINAL:
                t = t.model_copy(update={'status': 'cancelled', 'revision': t.revision + 1})
                old[i] = t
                cancelled_ids.append(t.task_id)
        if not decision.tasks:
            return old, cancelled_ids, None if cancelled_ids else '当前没有尚未完成的任务。'

    planned, refs, touched, targets = [], {}, list(cancelled_ids), set()
    for item in decision.tasks:
        previous = _target(item, old)
        if item.retry_failed:
            if previous is None or previous.status not in {'partial','failed'} or not retry_operations(previous):
                raise PlanConflict('没有可安全重试的失败只读操作。权限受限、已完成的操作和业务写入不会自动重试；请核对后重新提出具体请求。')
        if previous and previous.task_id in targets:
            raise PlanConflict('同一任务出现了多次修改，请明确每项任务的要求。')
        if previous:
            targets.add(previous.task_id)
        if item.cancel and previous is None:
            raise PlanConflict('没有找到要取消的任务，请说明要取消哪一项。')
        if previous and previous.status == 'cancelled' and not item.cancel:
            raise PlanConflict('该任务已取消，请提出一项新的请求。')
        # An explicit target is the only source of parameter inheritance in a multi-task context.
        values = item.model_dump(include=set(RouteDecision.model_fields))
        values['continuation'] = bool(previous)
        if item.retry_failed:
            # "Retry payment" may be labelled payment_query by the model, but its failed
            # operation still belongs to the original compound order task. The ledger owns it.
            values['task_type']=previous.kind
            values['next']=previous.agent
            values['focus']=list(previous.focus)
        if item.task_type == 'product_detail' and not item.parameters.product_ids and item.parameters.keyword and not item.depends_on:
            # Names/model families identify a search, whereas get_product_info direct tasks require IDs.
            values['task_type'] = 'product_search'
        task = prepare_task(RouteDecision.model_validate(values), previous.model_dump() if previous else None)
        if item.retry_failed:
            allowed = None
            if task.parameters != previous.parameters:
                # A model can express "the failed one" as a narrower set of objects.
                # Accept only an exact subset of observed failed read operations, never
                # changed conditions/new objects. Keep original task parameters/results.
                from task_execution import read_operations
                from task_operations import operation_id
                proposed = read_operations(task)
                eligible = {operation_id(n,a) for n,a in retry_operations(previous)}
                candidate = {operation_id(n,a) for n,a in proposed} if proposed else set()
                if candidate and candidate <= eligible:
                    allowed = list(candidate)
            if task.kind!=previous.kind or (task.parameters!=previous.parameters and allowed is None):
                raise PlanConflict('重试仅沿用原任务条件；如要更换订单、商品或筛选条件，请作为新查询提出。')
            task.parameters=previous.parameters.model_copy(deep=True)
            task.operations=[o.model_copy(deep=True) for o in previous.operations]
            task.retry_failed=True
            task.retry_operation_ids=allowed or []
            task.focus=list(previous.focus)
        elif previous and previous.status=='partial' and not item.cancel:
            # A new narrower query must not silently erase the outstanding part of an older task.
            from uuid import uuid4
            task.task_id=uuid4().hex
            task.revision=1
        if previous:
            task.evidence_call_ids = list(previous.evidence_call_ids)
            task.candidate_product_ids = list(previous.candidate_product_ids)
            task.displayed_product_ids = list(previous.displayed_product_ids)
            if task.agent == 'sales' and task.parameters.product_position is not None:
                position = task.parameters.product_position
                ordered = previous.displayed_product_ids
                task.parameters.product_ids = [ordered[position - 1]] if position <= len(ordered) else []
                task.parameters.product_position = None
                task.awaiting_fields = missing_fields(task)
                task.status = 'awaiting_input' if task.awaiting_fields else 'ready'
        if previous and item.cancel:
            task = previous.model_copy(deep=True)
            task.status = 'cancelled'
            task.revision += 1
        task.query = item.query or query
        if task.kind == 'product_search' and not item.retry_failed:
            from task_state import explicit_product_count
            count=explicit_product_count(task.query)
            if count is not None:task.parameters.limit=count
        if item.depends_on:
            task.depends_on = list(item.depends_on)
            task.dependency_mode = item.dependency_mode
        elif previous and previous.kind == task.kind:
            task.depends_on = previous.depends_on
            task.dependency_mode = previous.dependency_mode
        if task.depends_on and task.status != 'cancelled' and not task.retry_failed:
            task.status = 'awaiting_dependency'
        refs[item.ref] = task.task_id
        planned.append(task)
        touched.append(task.task_id)

    replacements = {t.task_id: t for t in planned}
    combined = [replacements.get(t.task_id, t) for t in old]
    combined += [t for t in planned if t.task_id not in {v.task_id for v in old}]
    known = {t.task_id for t in combined}
    for t in planned:
        t.depends_on = [refs.get(key, key) for key in t.depends_on]
        if any(key not in known or key == t.task_id for key in t.depends_on):
            raise PlanConflict('任务之间的依赖关系不明确，请分别说明处理顺序。')
        if t.dependency_mode != 'none' and len(t.depends_on) != 1:
            raise PlanConflict('商品信息的来源任务必须明确。')
        if t.dependency_mode == 'order_products' and t.kind != 'product_detail':
            raise PlanConflict('订单商品交接只用于商品详情查询。')
        if t.dependency_mode == 'selected_product' and t.kind not in {'quote', 'product_detail'}:
            raise PlanConflict('商品选择的后续操作不明确。')
    by_id = {t.task_id: t for t in combined}
    def visit(key, chain):
        if key in chain:
            raise PlanConflict('任务之间存在循环依赖，请说明正确的处理顺序。')
        if len(chain) >= 4:
            raise PlanConflict('任务依赖层数过多，请分步提出请求。')
        for dep in by_id[key].depends_on:
            if dep not in by_id:
                raise PlanConflict('依赖任务不存在。')
            visit(dep, chain | {key})
    for t in combined:
        visit(t.task_id, set())
    # A resumed prerequisite can awaken dependent tasks from a previous turn.
    changed = True
    while changed:
        changed = False
        for t in combined:
            if t.status == 'awaiting_dependency' and any(key in touched for key in t.depends_on) and t.task_id not in touched:
                touched.append(t.task_id)
                changed = True
    if sum(t.status not in TERMINAL for t in combined) > MAX_OPEN_TASKS:
        raise PlanConflict('尚未完成的任务较多，请先完成或取消其中一些任务。')
    needed = set(touched) | {t.task_id for t in combined if t.status not in TERMINAL}
    while True:
        expanded = needed | {dep for t in combined if t.task_id in needed for dep in t.depends_on}
        if expanded == needed:
            break
        needed = expanded
    if len(needed) > MAX_SAVED_TASKS:
        raise PlanConflict('需要保留的关联任务较多，请先完成或取消部分任务。')
    keep = [t for t in combined if t.task_id in needed]
    recent = [t for t in combined if t.task_id not in needed]
    combined = recent[-max(0, MAX_SAVED_TASKS-len(keep)):] + keep if len(keep) < MAX_SAVED_TASKS else keep
    return combined, touched, None


def task_evidence(task, messages):
    ids = set(task.evidence_call_ids)
    return [e for e in collect_evidence(messages) if e.call_id in ids]


def resolve_dependencies(task, by_id, messages):
    if not task.depends_on or task.status != 'awaiting_dependency':
        return task
    parents = [by_id[key] for key in task.depends_on]
    if task.dependency_mode=='none':
        if any(p.status!='completed' for p in parents):
            return task
    elif any(p.status not in {'completed','partial'} for p in parents):
        return task
    task = task.model_copy(deep=True)
    if task.dependency_mode == 'order_products':
        parent = parents[0]
        products, order_sequence = set(), []
        for e in task_evidence(parent, messages):
            if e.tool == 'query_order' and e.data.get('success') is True:
                for row in e.data.get('items', []):
                    if type(row.get('product_id')) is int:
                        products.add(row['product_id'])
                        order_sequence.append(row['product_id'])
        if not any(e.tool=='query_order' and e.data.get('success') is True for e in task_evidence(parent,messages)):
            return task
        # IDs come exclusively from an authorized successful order lookup, never the plan.
        requested = task.parameters.product_ids
        if task.parameters.product_position is not None:
            position = task.parameters.product_position
            task.parameters.product_ids = [order_sequence[position - 1]] if position <= len(order_sequence) else []
            task.parameters.product_position = None
        elif len(products) > 6:
            # Do not truncate a large order and silently claim every product was introduced.
            task.candidate_product_ids = sorted(products)
            if requested and len(requested) <= 6 and all(pid in products for pid in requested):
                task.parameters.product_ids = requested
            else:
                task.parameters.product_ids = []
        else:
            task.parameters.product_ids = sorted(products)
    elif task.dependency_mode == 'selected_product':
        parent = parents[0]
        verified = set(parent.candidate_product_ids)
        if task.parameters.product_position is not None:
            position = task.parameters.product_position
            ordered = parent.displayed_product_ids
            task.parameters.product_ids = [ordered[position - 1]] if position <= len(ordered) and ordered[position - 1] in verified else []
            task.parameters.product_position = None
        else:
            task.parameters.product_ids = [pid for pid in parent.parameters.product_ids if pid in verified]
    task.awaiting_fields = missing_fields(task)
    task.status = 'awaiting_input' if task.awaiting_fields else 'ready'
    return task


def choose_task(tasks, turn_ids, messages, steps):
    by_id = {t.task_id: t for t in tasks}
    resolved = [resolve_dependencies(t, by_id, messages) if t.task_id in turn_ids else t for t in tasks]
    if steps >= 4:
        return resolved, None
    for t in resolved:
        if t.task_id in turn_ids and t.status == 'ready':
            return resolved, t
    return resolved, None


def scoped_history(messages, task):
    """Give an expert public context and its own paired tool evidence, not another task's bodies."""
    out, allowed = [], set(task.evidence_call_ids)
    for m in messages:
        if getattr(m, 'type', None) == 'tool':
            if m.tool_call_id in allowed:
                out.append(m)
        elif isinstance(m, AIMessage) and m.tool_calls:
            calls = [c for c in m.tool_calls if c['id'] in allowed]
            if calls:
                out.append(m.model_copy(update={'tool_calls': calls}))
        else:
            out.append(m)
    # Replacing only the last user message isolates a compound request into the active subtask.
    from langchain_core.messages import HumanMessage
    last = max((i for i, m in enumerate(out) if isinstance(m, HumanMessage)), default=-1)
    if last >= 0:
        out[last] = HumanMessage(content=task.query or out[last].content)
    return out
