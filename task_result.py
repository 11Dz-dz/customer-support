"""统一工具证据、任务完成状态和回答输入；展示层不直接解析整段聊天历史。"""
import json
from typing import Any, Literal

from pydantic import BaseModel, Field
from task_state import OperationRecord
from task_operations import DENIAL_CODES, update_operations
from tool_result import ToolResult, normalize_result


class ToolEvidence(BaseModel):
    tool: str
    call_id: str | None = None
    data: dict[str, Any]
    arguments: dict[str, Any] | None = None
    result: ToolResult | None = None


class TaskResult(BaseModel):
    task_id: str | None = None
    status: Literal['completed', 'partial', 'empty', 'awaiting_input', 'denied', 'failed', 'cancelled'] = 'completed'
    current: list[ToolEvidence] = Field(default_factory=list)
    history: list[ToolEvidence] = Field(default_factory=list)
    awaiting_fields: list[str] = Field(default_factory=list)
    from_history: bool = False
    requested_order_id: int | None = None
    product_category: str | None = None
    retained: list[ToolEvidence] = Field(default_factory=list)
    operations: list[OperationRecord] = Field(default_factory=list)
    is_retry: bool = False


def collect_evidence(messages):
    rows, calls = [], {}
    for message in messages:
        for call in getattr(message,'tool_calls',[]) or []:
            calls[call['id']]=call
        if getattr(message, 'type', None) != 'tool':
            continue
        name=getattr(message,'name',None) or ''
        result=normalize_result(message.content,name,transport_error=getattr(message,'status',None)=='error')
        rows.append(ToolEvidence(tool=name,call_id=getattr(message,'tool_call_id',None),data=result.payload(),result=result,
                                 arguments=calls.get(getattr(message,'tool_call_id',None),{}).get('args')))
    return rows


def collect_task_result(messages, task=None):
    last_user = max((i for i, m in enumerate(messages) if getattr(m, 'type', None) == 'human'), default=-1)
    current = collect_evidence(messages[last_user + 1:])
    history = collect_evidence(messages[:last_user + 1])
    is_retry=bool(task and task.retry_failed)
    previous=task.operations if is_retry else []
    operations=update_operations(previous,current)
    retained_ids={o.call_id for o in previous if o.status=='succeeded'}
    retained=[e for e in history if e.call_id in retained_ids and e.data.get('success') is True]
    effective=retained+current
    status = 'completed'
    errors = [e for e in effective if e.data.get('success') is False]
    successful = [e for e in effective if e.data.get('success') is True]
    failed_operations=[o for o in operations if o.status!='succeeded']
    if any(e.data.get('error_code') in DENIAL_CODES for e in errors) or any(o.status=='denied' for o in failed_operations):
        status = 'denied'
        retained=[]
    elif (errors or failed_operations) and successful:
        status = 'partial'
    elif errors or failed_operations:
        status = 'failed'
    elif successful and all(e.result is not None and e.result.status=='empty' for e in successful):
        status = 'empty'
    if task and task.status in {'awaiting_input', 'cancelled'}:
        status = task.status
    return TaskResult(task_id=task.task_id if task else None, status=status, current=current,
                      history=history, awaiting_fields=task.awaiting_fields if task else [],
                      from_history=not current, requested_order_id=task.parameters.order_id if task else None,
                      product_category=task.parameters.product_category if task and task.kind=='product_search' else None,
                      retained=retained,operations=operations,is_retry=is_retry)


def evidence_pairs(rows):
    return [(row.tool, row.data) for row in rows]
