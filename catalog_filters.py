"""AdventureWorks catalog facets, based on imported product_subcategory_id metadata.

This mapping belongs to this dataset adapter; a real catalog must supply its own taxonomy.
Unknown/null subcategories are never silently assigned to a category.
"""
from typing import Literal

ProductCategory = Literal['all','bicycles','components','clothing','accessories']
CATEGORY_SUBCATEGORIES={'bicycles':(1,2,3),'components':tuple(range(4,18)),
                        'clothing':tuple(range(18,26)),'accessories':tuple(range(26,38))}


def category_conditions(category=None, *, alias=''):
    if category is None or category=='all':return [],[]
    if category not in CATEGORY_SUBCATEGORIES:raise ValueError('不支持的商品类别')
    if alias not in {'','p.'}:raise ValueError('不支持的字段前缀')
    return [alias+'product_subcategory_id = ANY(%s)'],[list(CATEGORY_SUBCATEGORIES[category])]


def category_matches(product,category=None):
    return category in (None,'all') or product.get('product_subcategory_id') in CATEGORY_SUBCATEGORIES.get(category,())
