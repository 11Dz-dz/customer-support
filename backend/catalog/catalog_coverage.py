"""Read-only catalogue evidence. Text hits never establish a product type."""
import re
from backend.postgres_config import connect_database
from backend.catalog.product_taxonomy import PRODUCT_TYPES
from backend.catalog.catalog_filters import CATEGORY_SUBCATEGORIES

CATEGORY_ALIASES = {'自行车': 'bicycles', 'bicycle': 'bicycles', 'bikes': 'bicycles',
                    'bike': 'bicycles', '零部件': 'components', '骑行服装': 'clothing', '骑行装备': 'accessories'}


def object_filter(item):
    """Retain only a taxonomy-confirmed object when coverage becomes a list."""
    from backend.catalog.catalog_query import CatalogExpression,CatalogGroup,CatalogPredicate
    normalized=(item or '').strip().casefold()
    if normalized in CATEGORY_ALIASES:
        field,value='category',CATEGORY_ALIASES[normalized]
    else:
        key=next((key for key,(_,label,names) in PRODUCT_TYPES.items()
                  if normalized in {key.casefold(),label.casefold(),*(name.casefold() for name in names)}),None)
        if key is None:return None
        field,value='product_type',key
    return CatalogExpression(groups=[CatalogGroup(conditions=[CatalogPredicate(field=field,operator='eq',values=[value])])])


def lexical_match(term, text):
    pattern = re.escape(term.strip())
    if term.isascii():
        pattern = r'(?<![A-Za-z0-9])' + pattern + r'(?![A-Za-z0-9])'
    return bool(pattern and re.search(pattern, text or '', re.I))


def coverage(requested_item, search_terms=None, source_text=None):
    item = (requested_item or '').strip()
    if not item or len(item) > 100:
        return failed()
    ids = set(CATEGORY_SUBCATEGORIES.get(CATEGORY_ALIASES.get(item.casefold()), []))
    for key, (subcategories, label, names) in PRODUCT_TYPES.items():
        if item.casefold() in {key.casefold(), label.casefold(), *(name.casefold() for name in names)}:
            ids.update(subcategories)
    terms = list(dict.fromkeys([item, *(search_terms or [])]))[:9]
    terms = [term.strip() for term in terms if isinstance(term, str) and term.strip() and len(term) <= 100]
    try:
        with connect_database() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            rows = conn.execute('SELECT product_id,name,product_number,product_subcategory_id,description_en,description_zh FROM product_catalog').fetchall()
    except Exception:
        return failed()
    fields = ['name', 'product_number', 'description_en', 'description_zh']
    confirmed = [row for row in rows if row['product_subcategory_id'] in ids]
    hits = [row for row in rows if any(lexical_match(term, str(row.get(field) or ''))
                                     for term in terms for field in fields)]
    unknown = [row for row in rows if row['product_subcategory_id'] is None]
    status = 'CONFIRMED' if confirmed else 'RELATED_ONLY' if hits else 'NO_EVIDENCE'
    return {'success': True, 'operation': 'coverage', 'coverage_status': status,
            'template_id': 'catalog_coverage_' + status.lower(), 'requested_item': item,
            'terms': [{'text': term, 'origin': 'user_original' if term == item or source_text and term in source_text else 'model_suggestion'} for term in terms],
            'checked_fields': fields, 'checked_records': len(rows), 'confirmed_count': len(confirmed),
            'lexical_hit_count': len(hits), 'unclassified_checked': len(unknown),
            'unclassified_hits': sum(row['product_subcategory_id'] is None for row in hits),
            'unclassified_missing_description': sum(not row['description_en'] and not row['description_zh'] for row in unknown),
            'products': [], 'returned_count': 0, 'matched_count': len(confirmed), 'query_scope': 'catalog_coverage_evidence'}


def failed():
    return {'success': False, 'operation': 'coverage', 'coverage_status': 'CHECK_FAILED',
            'template_id': 'catalog_coverage_check_failed', 'error_code': 'CATALOG_COVERAGE_FAILED',
            'tool_message': '本次目录覆盖检查未完成，暂时无法确认相关商品信息，请稍后重试。'}


def render_coverage(data):
    item = data['requested_item']
    status = data['coverage_status']
    if status == 'CONFIRMED':
        text = f'当前历史样例目录已记录“{item}”对应商品类型，共{data["confirmed_count"]}条记录。'
    elif status == 'RELATED_ONLY':
        text = f'当前目录检索到{data["lexical_hit_count"]}条文字相关记录，尚不能据此确认属于“{item}”。'
    else:
        text = f'本次在目录商品名称、型号及已提供的中英文描述中，未找到“{item}”的匹配证据。'
    text += '\n检索词：' + '、'.join(term['text'] for term in data['terms']) + '。'
    text += f'\n已检查{data["checked_records"]}条记录，包括{data["unclassified_checked"]}条未分类记录；其中{data["unclassified_missing_description"]}条缺少描述，仅能检查名称、型号等现有字段。'
    return text + '\n目录主要包含自行车、零部件、骑行服装和骑行装备。以上为历史样例目录证据，实时库存和在售状态无法确认。'
