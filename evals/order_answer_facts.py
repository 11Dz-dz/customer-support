"""Conservative, deterministic order/field/value checks for evaluation only.

Supported labels and order headings are explicit. Unparsed required facts fail
coverage instead of silently passing. This is not a general natural-language parser.
"""
from decimal import Decimal, InvalidOperation
import re

VERSION='1.0.0'
LABELS={
    'total_due':r'订单应付总额|订单总金额|订单总额|应付总额|应付金额|总金额',
    'subtotal':r'商品小计|商品金额小计',
    'tax_amount':r'税费|税额',
    'freight':r'运费',
}
ORDER=re.compile(r'(?:订单(?:号|编号)?\s*(?:为|是)?\s*[:：#]?\s*|\b)(SO\d+)|订单(?:号|编号)?\s*(?:为|是)?\s*[:：#]?\s*(\d+)',re.I)
MONEY=r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
LABEL_ANY=re.compile('|'.join(LABELS.values()))
FIELD=re.compile(r'(?P<label>'+ '|'.join('(?:'+v+')' for v in LABELS.values())+
    r')\s*(?:为|是|[:：=])?\s*(?P<amount>'+MONEY+r')(?![\d.,])')


def normalize_id(value):
    raw=str(value).strip().upper().removeprefix('SO')
    return int(raw) if raw.isdecimal() else None


def result(name,passed,detail=None,critical=False):
    return {'name':name,'passed':bool(passed),'detail':detail,'dimension':'answer_facts','critical':critical}


def parse_amount_claims(answer,default_order=None):
    """Order headings scope later lines until the next heading; inline headings work too."""
    clean=answer.replace('**','').replace('`','').replace('，',',')
    anchors=list(ORDER.finditer(clean))
    claims=[]
    for match in FIELD.finditer(clean):
        previous=[a for a in anchors if a.end()<=match.start()]
        oid=normalize_id(next(g for g in previous[-1].groups() if g)) if previous else default_order
        label=match['label']
        field=next(k for k,v in LABELS.items() if re.fullmatch(v,label))
        claims.append({'order_id':oid,'field':field,'value':match['amount'].replace(',',''),
                       'text':match.group(),'offset':match.start()})
    return claims


def answer_fact_checks(answer,references,required=(),*,default_order=None):
    """References come from independent SQL, requirements from the case contract.

    Wrong values are factual failures. Missing/unsupported phrasing is a coverage
    failure for review, not a fabricated assertion that the answer is factually wrong.
    """
    refs={normalize_id(k):v for k,v in references.items()}
    if default_order is None and len(refs)==1:default_order=next(iter(refs))
    claims=parse_amount_claims(answer,default_order)
    checks=[];seen=set()
    for claim in claims:
        oid,field=claim['order_id'],claim['field']
        ref=refs.get(oid)
        if ref is None:
            checks.append(result('amount_order_is_grounded',False,claim,critical=oid is not None))
            continue
        if field not in ref or ref[field] is None:
            checks.append(result('amount_field_is_grounded',False,claim,critical=True));continue
        try:
            actual=Decimal(claim['value']);expected=Decimal(str(ref[field]))
            ok=actual.is_finite() and expected.is_finite() and abs(actual-expected)<=Decimal('.01')
        except (InvalidOperation,TypeError,ValueError):ok=False
        checks.append(result('answer_order_'+field,ok,{**claim,'expected':str(ref[field])},critical=True))
        if ok:seen.add((oid,field))
    for item in required:
        oid=normalize_id(item['order_id']);field=item['field']
        checks.append(result('required_order_fact_present',(oid,field) in seen,
            {'order_id':oid,'field':field,'issue':'missing_or_unparsed_if_failed'}))
    # Detect unsupported labeled amounts as lack of parser coverage, not a pass.
    parsed_starts={c['offset'] for c in claims}
    unsupported=[m.group() for m in LABEL_ANY.finditer(answer.replace('**','').replace('`','').replace('，',','))
                 if m.start() not in parsed_starts]
    # Unknown statements may legitimately contain these labels. They are recorded,
    # and become failures only when the case actually requires a numerical answer.
    if unsupported and required:
        checks.append(result('amount_wording_review',not any(
            (normalize_id(r['order_id']),r['field']) not in seen for r in required),{'unparsed_labels':unsupported}))
    return checks
