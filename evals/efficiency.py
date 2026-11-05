"""Explicit contract exception for one grounded, public first-item detail read."""
from copy import deepcopy


def order_detail_warning(checks, trace, spec):
    if not spec.get('contract',{}).get('allow_first_order_detail_warning'):
        return checks
    tools=trace.get('tools',[])
    if len(tools)!=2 or [tool['name'] for tool in tools]!=['query_order','get_product_info']:
        return checks
    order,detail=tools
    if not all(tool.get('success') for tool in tools) or trace.get('violations'):
        return checks
    items=order.get('result',{}).get('items',[])
    if not items:return checks
    expected=items[0].get('product_id')
    product=detail.get('result',{}).get('product',{})
    if (expected is None or str(detail.get('args',{}).get('product_name'))!=str(expected)
            or product.get('product_id')!=expected):
        return checks
    eligible={'route','allowed_tools','agentevals_allowed_executions'}
    # Ownership, factual accuracy, writes, timeouts and execution limits retain
    # their original severity, even when this optional public read is grounded.
    if any(not check['passed'] and check['name'] not in eligible for check in checks):
        return checks
    updated=[]
    for check in checks:
        if not check['passed'] and check['name'] in eligible:
            updated.append({**check,'passed':True,'critical':False,'severity':'warning',
                'dimension':'efficiency','warning_code':'REDUNDANT_PUBLIC_ORDER_DETAIL',
                'original_check':deepcopy(check),'product_id':expected})
        else:updated.append(check)
    return updated
