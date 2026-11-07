"""AdventureWorks object taxonomy shared by planning, SQL and result checks."""
import re

# Dataset adapter metadata, not categories inferred from product descriptions.
PRODUCT_TYPES={
 'mountain_bike':((1,), '山地车', ('山地车','山地自行车','mountain bike')),
 'road_bike':((2,), '公路车', ('公路车','公路自行车','road bike')),
 'touring_bike':((3,), '旅行自行车', ('旅行自行车','旅行车','touring bike')),
 'handlebar':((4,), '车把', ('车把','把手','handlebar')),
 'bottom_bracket':((5,), '中轴', ('中轴','bottom bracket')),
 'brake':((6,), '刹车', ('刹车','制动器','brake')),
 'chain':((7,), '链条', ('链条','chain')),
 'crankset':((8,), '曲柄组', ('曲柄','牙盘','crankset')),
 'derailleur':((9,), '变速器', ('变速器','derailleur')),
 'fork':((10,), '前叉', ('前叉','fork')),
 'headset':((11,), '碗组', ('碗组','headset')),
 'frame':((12,14,16), '车架', ('车架','frame')),
 'pedal':((13,), '脚踏', ('脚踏','踏板','pedal')),
 'saddle':((15,), '车座', ('车座','坐垫','鞍座','saddle','seat')),
 'wheel':((17,), '车轮', ('车轮','轮组','wheel')),
 'bib_shorts':((18,), '背带骑行裤', ('背带骑行裤','bib shorts')),
 'cap':((19,), '帽子', ('帽子','骑行帽','cap')),
 'glove':((20,), '手套', ('手套','glove')),
 'jersey':((21,), '骑行上衣', ('骑行上衣','运动衫','jersey')),
 'shorts':((22,), '骑行短裤', ('骑行短裤','短裤','shorts')),
 'sock':((23,), '袜子', ('袜子','sock')),
 'tights':((24,), '紧身裤', ('紧身裤','tights')),
 'vest':((25,), '背心', ('背心','vest')),
 'rack':((26,), '自行车架', ('自行车架','载物架','bike rack')),
 'stand':((27,), '停车支架', ('停车支架','维修架','bike stand')),
 'bottle':((28,), '水壶及水壶架', ('水壶架','水壶','水瓶','bottle cage','water bottle')),
 'cleaner':((29,), '清洁用品', ('清洁剂','清洁用品','cleaner')),
 'fender':((30,), '挡泥板', ('挡泥板','fender')),
 'helmet':((31,), '头盔', ('自行车头盔','骑行头盔','头盔','helmet')),
 'hydration_pack':((32,), '水袋背包', ('水袋背包','hydration pack')),
 'light':((33,), '车灯', ('车灯','自行车灯','bike light')),
 'lock':((34,), '车锁', ('车锁','自行车锁','bike lock')),
 'pannier':((35,), '驮包', ('驮包','pannier')),
 'pump':((36,), '打气筒', ('打气筒','气筒','pump')),
 'tire_tube':((37,), '轮胎及内胎', ('轮胎','内胎','tire','tyre','tube')),
}


def mentions(text, *, positive_only=False):
    found=[]
    for key,(_,label,aliases) in PRODUCT_TYPES.items():
        for alias in aliases:
            pattern=re.escape(alias)
            if alias.isascii():pattern=r'(?<![A-Za-z])'+pattern+r's?(?![A-Za-z])'
            for m in re.finditer(pattern,text,re.I):found.append((m.start(),m.end(),key))
    selected=[]
    # In Chinese compounds the final part is the object: 旅行车把 is a
    # handlebar for touring, not a touring bicycle. Prefer a component head
    # extending beyond an overlapping bicycle-type modifier.
    bike_types={'mountain_bike','road_bike','touring_bike'}
    found=[r for r in found if r[2] not in bike_types or not any(
        other[2] not in bike_types and other[0]<r[1] and r[0]<other[1] and other[1]>r[1] for other in found)]
    for row in sorted(found,key=lambda r:(-(r[1]-r[0]),r[0])):
        if any(row[0]<b and a<row[1] for a,b,_ in selected):continue
        selected.append(row)
    if positive_only:
        selected=[r for r in selected if not re.search(
            r'(?:不要|不看|排除|不买|不选|除了|除去|不含|不是|非|不再限于|不再限|不限于|不限)\s*(?:红色|黑色|银色|蓝色)?\s*$',text[max(0,r[0]-12):r[0]])]
    return sorted(selected)


def capabilities():
    return {key:{'label':label,'aliases':list(aliases)} for key,(_,label,aliases) in PRODUCT_TYPES.items()}


def normalize_object(expression,text,*,trusted=None):
    """Resolve one explicit object; multi-object logic stays with the typed AST."""
    from backend.catalog.catalog_query import CatalogExpression,CatalogGroup,CatalogPredicate,fields
    from backend.tasks.constraint_updates import ConstraintConflict
    cited={key for _,_,key in mentions(text)}
    known={c.model_dump_json() for g in trusted.groups for c in g.conditions if c.field=='product_type'} if trusted else set()
    for group in expression.groups:
        for c in group.conditions:
            if c.field=='product_type' and not set(c.values)<=cited and c.model_dump_json() not in known:
                raise ConstraintConflict('catalog_filter','商品类型在引用原文中没有对应对象',repairable=True)
    positive={key for _,_,key in mentions(text,positive_only=True)}
    from backend.catalog.catalog_filters import CATEGORY_SUBCATEGORIES
    for group in expression.groups:
        for c in group.conditions:
            if c.field=='product_type' and c.operator in {'eq','in'} and not set(c.values)<=positive and (set(c.values)&cited or c.model_dump_json() not in known):
                raise ConstraintConflict('catalog_filter','被排除或取消限制的商品类型不能设为正向范围',repairable=True)
    if len(positive)==1 and len(cited)==1 and 'product_type' not in fields(expression):
        pred=CatalogPredicate(field='product_type',operator='eq',values=list(positive))
        expression=CatalogExpression(groups=[CatalogGroup(conditions=g.conditions+[pred]) for g in expression.groups]
            or [CatalogGroup(conditions=[pred])])
    groups=[]
    for g in expression.groups:
        conditions=list(g.conditions)
        types=[c for c in conditions if c.field=='product_type' and c.operator in {'eq','in'}]
        if types:
            ids={n for c in types for v in c.values for n in PRODUCT_TYPES[v][0]}
            categories={k for k,v in CATEGORY_SUBCATEGORIES.items() if ids&set(v)}
            positive_categories=[c for c in conditions if c.field=='category' and c.operator in {'eq','in'}]
            if positive_categories and any(not ids&{n for v in c.values for n in CATEGORY_SUBCATEGORIES[v]} for c in positive_categories):
                raise ConstraintConflict('catalog_filter','具体商品类型与候选大类不一致',repairable=True)
            if not any(c.field=='category' for c in conditions):
                conditions.append(CatalogPredicate(field='category',operator='in',values=sorted(categories)))
        groups.append(CatalogGroup(conditions=conditions))
    expression=CatalogExpression(groups=groups)
    return expression


def object_only_keyword(keyword,expression):
    from backend.catalog.catalog_query import fields
    if not keyword or 'product_type' not in fields(expression):return False
    words={alias.casefold() for _,_,aliases in PRODUCT_TYPES.values() for alias in aliases}
    return keyword.strip().casefold() in words
