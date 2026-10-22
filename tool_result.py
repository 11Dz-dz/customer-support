"""Versioned tool boundary. Business states live in data; execution outcomes live outside.

Legacy flat dictionaries are decoded for saved history and service adapters only.
Unknown failures are never guessed from their human-readable text or retried by default.
"""
from functools import wraps
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator, ValidationError

ToolStatus=Literal['success','empty','invalid_argument','forbidden','temporary_error','error']
DENIAL_CODES={'SESSION_UNBOUND','ORDER_NOT_ACCESSIBLE','SESSION_IDENTITY_CHANGED','IDENTITY_MISMATCH',
    'SESSION_BINDING_MISMATCH','TASK_TOOL_NOT_ALLOWED','SESSION_IDENTITY_CONFLICT','SESSION_IDENTITY_ERROR',
    'RETRY_TOOL_NOT_ALLOWED'}
TEMPORARY_CODES={'TEMPORARY_ERROR','DATABASE_UNAVAILABLE','DATABASE_TIMEOUT','TOOL_TIMEOUT','SERVICE_UNAVAILABLE','RATE_LIMITED','REQUEST_DEADLINE'}
EMPTY_CODES={'PRODUCT_NOT_FOUND','CATALOG_EMPTY','ERROR_CODE_NOT_FOUND'}
INVALID_CODES={'INVALID_TOOL_ARGUMENTS','INVALID_QUERY_OPTIONS','INVALID_LIMIT','INVALID_PRICE_FILTER',
    'INVALID_SEARCH_QUERY','INVALID_CATEGORY','INVALID_QUANTITY','INVALID_REFUND_REASON','INVALID_BUNDLES',
    'INVALID_BUDGET','INVALID_PRODUCT_IDENTIFIER','UNKNOWN_TOOL','UNSUPPORTED_COMPONENT'}
MESSAGES={
 'PRODUCT_NOT_FOUND':'没有找到该商品，请核对商品编号或名称。',
 'CATALOG_EMPTY':'没有符合当前条件的商品，请调整条件后查询。',
 'ERROR_CODE_NOT_FOUND':'当前错误码说明中没有该代码，请补充具体报错信息。',
 'INVALID_QUANTITY':'购买数量必须是1–10000的整数，请修改数量。',
 'INVALID_PRICE_FILTER':'价格条件无效，请检查非负金额及上下限。',
 'INVALID_REFUND_REASON':'请提供1–2000字符的退款原因。',
 'INVALID_BUNDLES':'报价方案格式或商品数量无效，请核对商品与购买件数。',
 'INVALID_BUDGET':'预算必须是有限的非负数，请修改预算。',
 'PRODUCT_NOT_QUOTABLE':'商品不存在或没有有效目录报价，本次未生成报价。',
 'ORDER_NOT_ACCESSIBLE':'订单不存在或当前会话无权查看，无法提供订单信息。',
 'SESSION_UNBOUND':'当前会话尚未绑定客户身份，暂不能访问订单。',
 'DATABASE_UNAVAILABLE':'数据库暂时无法连接，可以稍后重试该只读查询。',
 'DATABASE_TIMEOUT':'数据库查询超时，可以稍后重试该只读查询。',
 'TOOL_TIMEOUT':'本次查询超时，可以稍后重试该只读查询。',
 'REQUEST_DEADLINE':'本轮执行时间已用尽，本项结果尚未确认；已完成的其他结果会保留。',
 'SERVICE_UNAVAILABLE':'服务暂时不可用，可以稍后重试该只读查询。',
 'RATE_LIMITED':'服务请求频率受限，请稍后再试。',
 'MALFORMED_TOOL_RESULT':'工具返回的结果格式不完整，暂时无法核实本项结果。',
 'UNCLASSIFIED_TOOL_ERROR':'本项操作失败，原因尚未分类，请核对信息或联系维护人员。',
 'TOOL_EXECUTION_ERROR':'工具执行异常，暂时无法核实结果，请联系维护人员。',
 'DATABASE_CONFIGURATION_ERROR':'数据库配置或权限异常，请联系维护人员。',
 'WRITE_RETRY_NOT_ALLOWED':'本轮申请操作未成功确认，不再重复提交；需人工核对申请记录。',
 'TOOL_TRANSPORT_ERROR':'工具执行未成功，不能使用本次返回内容作为业务事实。',
}


def status_for(code):
    if code in DENIAL_CODES:return 'forbidden'
    if code in TEMPORARY_CODES:return 'temporary_error'
    if code in EMPTY_CODES:return 'empty'
    if code in INVALID_CODES:return 'invalid_argument'
    return 'error'


def message_for(code):
    status=status_for(code)
    return MESSAGES.get(code,{'forbidden':'本次操作不被允许，请核对会话权限或当前任务。',
        'invalid_argument':'查询参数无效，请核对条件后重新提出请求。',
        'error':'本项操作未成功，暂时无法核实结果，请联系维护人员。'}.get(status,'本项操作未完成。'))


class ToolResult(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True,strict=True)
    schema_version: Literal[1]=1
    status: ToolStatus
    error_code: str | None=Field(default=None,pattern=r'^[A-Z][A-Z0-9_]{0,63}$')
    message: str=Field(min_length=1,max_length=1000)
    data: dict[str,Any]=Field(default_factory=dict)
    retryable: StrictBool=False

    @model_validator(mode='before')
    @classmethod
    def integer_version(cls,value):
        if isinstance(value,dict) and type(value.get('schema_version',1)) is not int:raise ValueError('integer version required')
        return value

    @model_validator(mode='after')
    def coherent(self):
        if self.status=='success':
            if self.error_code is not None or self.retryable:raise ValueError('success has no error/retry')
        elif not self.error_code or status_for(self.error_code)!=self.status:
            raise ValueError('outcome and error code disagree')
        if self.retryable and self.error_code not in TEMPORARY_CODES:raise ValueError('unclassified retry')
        return self

    @property
    def ok(self):return self.status in {'success','empty'}

    def payload(self):
        """Compatibility view for existing evidence renderers; never persisted over old messages."""
        return {**self.data,'success':self.ok,'error_code':self.error_code,
                'tool_status':self.status,'tool_message':self.message,'tool_retryable':self.retryable,
                **({'error':self.message} if not self.ok else {})}


def failure(code,message=None,*,data=None):
    return ToolResult(status=status_for(code),error_code=code,message=message or message_for(code),
                      data=data or {},retryable=code in TEMPORARY_CODES)


def failure_payload(code,message=None,**data):
    """Internal service adapter shape; the public tool boundary wraps it exactly once."""
    return {'success':False,'error_code':code,'error':message or message_for(code),**data}


def exception_result(exc):
    # Check exact exception types / SQLSTATE, never exception-body strings.
    from psycopg import Error as DatabaseError, OperationalError
    from request_deadline import RequestDeadlineExceeded
    from openai import APITimeoutError, APIConnectionError, RateLimitError
    if isinstance(exc,RequestDeadlineExceeded):code='REQUEST_DEADLINE'
    elif isinstance(exc,ValidationError):code='INVALID_TOOL_ARGUMENTS'
    elif isinstance(exc,APITimeoutError):code='TOOL_TIMEOUT'
    elif isinstance(exc,APIConnectionError):code='SERVICE_UNAVAILABLE'
    elif isinstance(exc,RateLimitError):code='RATE_LIMITED'
    elif isinstance(exc,TimeoutError):code='TOOL_TIMEOUT'
    elif isinstance(exc,ConnectionError):code='SERVICE_UNAVAILABLE'
    elif isinstance(exc,DatabaseError):
        state=exc.sqlstate or ''
        if state.startswith(('28','3D','3F','42')):code='DATABASE_CONFIGURATION_ERROR'
        elif state=='57014':code='DATABASE_TIMEOUT'
        elif state.startswith('08') or state in {'40001','40P01','53300','57P01','57P02','57P03'} or isinstance(exc,OperationalError):code='DATABASE_UNAVAILABLE'
        else:code='TOOL_EXECUTION_ERROR'
    else:code='TOOL_EXECUTION_ERROR'
    return failure(code)


def normalize_result(value,tool='',*,transport_error=False):
    if isinstance(value,str):
        try:value=json.loads(value)
        except (TypeError,ValueError):
            value={'success':True,'text':value} if tool=='search_error_code' else None
    if isinstance(value,ToolResult):result=value
    elif isinstance(value,dict) and ('schema_version' in value or 'retryable' in value and 'data' in value):
        try:result=ToolResult.model_validate(value)
        except ValidationError:result=failure('MALFORMED_TOOL_RESULT')
    elif isinstance(value,dict) and type(value.get('success')) is bool:
        if value['success']:
            data={k:v for k,v in value.items() if k not in {'success','error_code','error','tool_status','tool_message','tool_retryable'}}
            empty=tool in {'search_products','query_products','get_product_info'} and 'products' in data and not data['products'] and not data.get('product') and not data.get('matched_count',0)
            result=failure('CATALOG_EMPTY',data=data) if empty else ToolResult(status='success',message='本项查询或操作已完成。',data=data)
        else:
            code=value.get('error_code') or 'UNCLASSIFIED_TOOL_ERROR'
            import re
            if not isinstance(code,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',code):code='UNCLASSIFIED_TOOL_ERROR'
            # Monitoring findings retain their safe diagnostic fields; other failed calls
            # cannot smuggle order/product payloads into a denied or malformed result.
            data={k:v for k,v in value.items() if k not in {'success','error_code','error'}} if tool=='check_system_status' else {}
            if tool=='check_system_status' and code=='HTTP_ERROR' and value.get('http_status') in {500,502,503,504}:code='SERVICE_UNAVAILABLE'
            result=failure(code,data=data)
    else:result=failure('MALFORMED_TOOL_RESULT')
    if not result.ok and tool!='check_system_status' and result.data:
        result=result.model_copy(update={'data':{}})
    return failure('TOOL_TRANSPORT_ERROR') if transport_error and result.ok else result


def business_payload(value,tool='',*,transport_error=False):
    return normalize_result(value,tool,transport_error=transport_error).payload()


def validate_business_data(result,tool):
    """Validate minimum factual fields at fresh production boundaries; keep old history readable."""
    if result.status=='empty':
        if tool in {'query_order','query_invoice','check_payment_status','request_refund','check_system_status'}:
            return failure('MALFORMED_TOOL_RESULT')
        if result.data.get('product') or result.data.get('matched_count',0)>0:
            return failure('MALFORMED_TOOL_RESULT')
    if result.status!='success':return result
    data=result.data
    required={
      'query_order':{'order_id':int,'status_code':int,'items':list},
      'check_payment_status':{'order_id':int,'payment_status':str},
      'query_invoice':{'order_id':int,'invoice_status':str},
      'request_refund':{'order_id':int,'refund_id':str,'status':str},
      'query_products':{'products':list,'matched_count':int,'returned_count':int},
      'search_products':{'products':list,'returned_count':int},
      'check_system_status':{'component':str,'status':str,'checked_at':str,'elapsed_ms':(int,float)},
      'search_error_code':{'text':str},
    }.get(tool,{})
    if any(key not in data or type(data[key]) not in (kind if isinstance(kind,tuple) else (kind,)) for key,kind in required.items()):
        return failure('MALFORMED_TOOL_RESULT')
    if tool=='get_product_info' and not (isinstance(data.get('product'),dict) and type(data['product'].get('product_id')) is int
            or data.get('needs_selection') is True and isinstance(data.get('products'),list)):
        return failure('MALFORMED_TOOL_RESULT')
    if tool=='calculate_price' and not (isinstance(data.get('bundles'),list)
            or type(data.get('quantity')) is int and 'subtotal' in data
            or data.get('needs_selection') is True and isinstance(data.get('products'),list)):
        return failure('MALFORMED_TOOL_RESULT')
    if tool=='check_promotion' and not (type(data.get('product_id')) is int and data.get('promotion_status')=='unknown'
            or data.get('needs_selection') is True and isinstance(data.get('products'),list)):
        return failure('MALFORMED_TOOL_RESULT')
    return result


def result_boundary(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        try:result=validate_business_data(normalize_result(function(*args,**kwargs),function.__name__),function.__name__)
        except Exception as exc:result=exception_result(exc)
        if function.__name__=='request_refund' and result.retryable:
            result=result.model_copy(update={'retryable':False,
                'message':'本次申请操作的结果尚未确认，需人工核对申请记录；程序不会自动再次提交。'})
        return result.model_dump(mode='json')
    return wrapped


def normalize_tool_message(message,tool):
    from langchain_core.messages import ToolMessage
    if not isinstance(message,ToolMessage):return normalize_result(message,tool).model_dump(mode='json')
    result=normalize_result(message.content,tool,transport_error=message.status=='error')
    return message.model_copy(update={'content':result.model_dump_json(),'status':'success' if result.ok else 'error'})
