"""Independent catalog oracle and conservative rendered-answer checks for double texting.

No search service, LLM judge, or business renderer is used to manufacture expected
answers. Unsupported answer formats fail coverage instead of silently passing.
"""
from decimal import Decimal, InvalidOperation
import re

VERSION='1.0.1'
BIKE_SUBCATEGORIES=(1,2,3)  # AdventureWorks dataset's documented taxonomy.
GENERIC_FALLBACK=('未能根据描述确认','请补充或缩小','暂时无法可靠','暂时无法生成',
                  '未能完整处理','执行异常','执行预算已用尽','规划等待超时','暂时无法可靠整理')
PRODUCT=re.compile(r'商品\s*(\d+)\s*[:：]')
PRICE=re.compile(r'历史目录标价\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)')


def catalog_oracle(connection,budget):
    """Read the full business catalog with SQL, independently of retrieval/ranking."""
    rows=connection.execute('''SELECT product_id,name,product_number,product_subcategory_id,
        list_price,description_zh,description_en FROM product_catalog
        WHERE product_subcategory_id IN (1,2,3) AND list_price>0 AND list_price<=%s
        ORDER BY product_id''',(Decimal(str(budget)),)).fetchall()
    return {'budget':str(budget),'category':'bicycles','quantity':1,
            'basis':'Independent SQL over all AdventureWorks bicycle subcategories 1,2,3; positive list price',
            'matched_count':len(rows),'products':rows}


def check(name,passed,detail=None):
    return {'name':name,'passed':bool(passed),'detail':detail}


def product_answer_checks(answer,oracle):
    """Validate this application's explicit product/price output grammar and empty answers."""
    text=answer.replace('**','').replace('`','')
    checks=[check('answer_present',bool(text.strip())),
            check('no_generic_fallback',not any(x in text for x in GENERIC_FALLBACK))]
    refs={int(p['product_id']):p for p in oracle['products']}
    anchors=list(PRODUCT.finditer(text))
    if not refs:
        # "No suitable recommendation confirmed" does not state that the catalog
        # has zero products under these constraints. Do not conflate those outcomes.
        negative=bool(re.search(r'(?:没有|未找到|未记录|找不到|暂无|不存在)[^。\n]{0,90}(?:符合|匹配|预算|自行车|整车)',text))
        scope=bool(re.search(r'(?:预算|价格|标价|当前条件|符合条件|匹配条件)',text))
        checks.extend([check('no_product_in_empty_catalog',not anchors),
                       check('explicit_no_matching_catalog_result',negative and scope),
                       check('no_unverified_inventory_claim',not bool(re.search(r'已售罄|缺货|没有库存|暂无库存',text)))])
        return checks
    checks.append(check('requested_product_count',len(anchors)==oracle['quantity'],
                        {'expected':oracle['quantity'],'actual':len(anchors)}))
    for index,anchor in enumerate(anchors):
        pid=int(anchor[1]);section=text[anchor.end():anchors[index+1].start() if index+1<len(anchors) else len(text)]
        ref=refs.get(pid)
        checks.append(check('selected_id_matches_independent_sql',ref is not None,{'product_id':pid}))
        if ref is None:continue
        checks.append(check('selected_name_matches_source',section.startswith(str(ref['name'])),{'product_id':pid}))
        prices=list(PRICE.finditer(section));price=prices[0][1] if len(prices)==1 else None
        try:correct=price is not None and Decimal(price)==Decimal(str(ref['list_price']))
        except InvalidOperation:correct=False
        checks.append(check('rendered_price_matches_source',correct,{'product_id':pid,'actual':price,'expected':str(ref['list_price'])}))
        checks.append(check('selected_is_bicycle_under_budget',ref['product_subcategory_id'] in BIKE_SUBCATEGORIES and
            0<Decimal(str(ref['list_price']))<=Decimal(oracle['budget']),{'product_id':pid}))
        quotes=re.findall(r'目录描述原文：“([^”]*)”',section)
        if quotes:
            checks.append(check('description_quote_matches_source',all(q in [ref.get('description_zh'),ref.get('description_en')] for q in quotes),{'product_id':pid}))
    return checks


def task_checks(tasks,budget):
    searches=[t for t in tasks if t.get('kind')=='product_search']
    latest=searches[-1] if searches else {};p=latest.get('parameters',{})
    return [check('task_is_product_recommendation',bool(searches)),
            check('final_budget_is_applied',p.get('max_price')==budget),
            check('bicycle_object_preserved',p.get('product_category')=='bicycles'),
            check('one_product_preserved',p.get('limit')==1),
            check('no_task_execution_failure',latest.get('failure_code') is None and bool(latest)),
            check('task_has_finished',latest.get('status') in {'completed','empty'})]


def outcome(checks):
    return {'passed':bool(checks) and all(c['passed'] for c in checks),
            'failed_checks':[c['name'] for c in checks if not c['passed']]}
