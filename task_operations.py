"""Operation identities and retry eligibility are computed from observed tool calls, never a model plan."""
import hashlib
import json

from task_state import OperationRecord
from tool_execution import READ_ONLY_TOOLS
from tool_result import DENIAL_CODES, TEMPORARY_CODES, normalize_result

NON_RETRYABLE_CODES = DENIAL_CODES | {'UNKNOWN_TOOL','INVALID_TOOL_ARGUMENTS','MALFORMED_TOOL_RESULT','RETRY_TOOL_NOT_ALLOWED'}
MAX_OPERATION_ATTEMPTS = 3
ROLE_TOOLS={'sales':{'query_products','search_products','get_product_info','check_promotion','calculate_price'},
            'billing':{'query_order','query_invoice','check_payment_status','request_refund'},
            'tech_support':{'check_system_status','search_error_code'}}


def operation_id(tool, arguments):
    payload=json.dumps([tool,arguments],sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def update_operations(previous, evidence):
    records={o.operation_id:o.model_copy(deep=True) for o in previous}
    for row in evidence:
        if row.arguments is None or row.call_id is None:
            continue
        key=operation_id(row.tool,row.arguments)
        old=records.get(key)
        # Cached duplicate messages have distinct calls but still consume the request budget.
        attempts=(old.attempts if old else 0)+1
        result=row.result or normalize_result(row.data,row.tool)
        ok=result.ok
        code=result.error_code
        denied=result.status=='forbidden'
        uncertain_write = (row.tool == 'request_refund' and result.status in {'temporary_error','error'}
            and code not in {'WRITE_RETRY_NOT_ALLOWED','TOOL_CALL_LIMIT','REQUEST_TOOL_LIMIT',
                             'TURN_DEADLINE','UNKNOWN_TOOL','REQUEST_SEARCH_LIMIT','SEARCH_CALL_LIMIT'})
        records[key]=OperationRecord(operation_id=key,tool=row.tool,arguments=row.arguments,
            status='succeeded' if ok else ('denied' if denied else 'failed'),call_id=row.call_id,
            attempts=attempts,error_code=code,outcome=result.status,
            requires_review=bool(uncertain_write or old and old.requires_review),
            retryable=result.retryable and row.tool in READ_ONLY_TOOLS
                      and attempts<MAX_OPERATION_ATTEMPTS)
    # Current per-task budgets permit at most 6 requests. Keep successful prerequisites if a
    # general agent explores new operations over later turns; older records remain in messages.
    return list(records.values())[-12:]


def retry_operations(task):
    return [(o.tool,dict(o.arguments)) for o in task.operations
            if o.status=='failed' and o.retryable and o.tool in READ_ONLY_TOOLS and o.tool in ROLE_TOOLS[task.agent]
            and o.error_code in TEMPORARY_CODES
            and (not task.retry_operation_ids or o.operation_id in task.retry_operation_ids)]


def operation_labels(operations):
    names={'query_order':'订单查询','check_payment_status':'支付查询','query_invoice':'发票查询',
           'get_product_info':'商品详情','check_promotion':'优惠查询','calculate_price':'报价计算',
           'query_products':'商品筛选','search_products':'商品检索','request_refund':'退款申请',
           'check_system_status':'系统检查','search_error_code':'错误码查询'}
    return '、'.join(dict.fromkeys(names.get(o.tool,'工具查询') for o in operations))
