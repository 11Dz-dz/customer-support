"""Versioned post-fix regression. Original broad dataset/reports stay immutable."""
from copy import deepcopy
from evals.broad_cases import broad_cases,turn,BIKES


def cases():
    data=deepcopy(broad_cases())
    for c in data:
        if c['id'] in {'EV_OR07','EV_MX02'}:
            c['turns'][1]['contract'].pop('required_order_facts',None)
        if c['id']=='EV_BD15':
            c['turns'][0]['input']='本客服系统网页出现HTTP 403，代表什么？'
    extra=[
      ('RF_TYPE01','列出三款helmet，按目录标价升序。',"product_subcategory_id=31",'ascending',3),
      ('RF_TYPE02','找两款车把，价格不超过100。',"product_subcategory_id=4 AND list_price<=100",'subset',2),
      ('RF_TYPE03','查骑行袜子，标价20以内，给三条。',"product_subcategory_id=23 AND list_price<=20",'subset',3),
      ('RF_TYPE04','不看头盔，列出最便宜的五条骑行装备。',"product_subcategory_id BETWEEN 26 AND 37 AND product_subcategory_id<>31",'ascending',5),
      ('RF_TYPE05','骑行头盔标价50以内共有多少条？',"product_subcategory_id=31 AND list_price<=50",'count',None),
    ]
    def add(cid,turns):
        data.append({'id':cid,'category':'postfix_generalization','family':cid,'split':'dev','actor':'owner','smoke':False,
            'expected_refunds':0,'fixture_refunds':{},'manual_review_required':True,'turns':turns})
    for cid,text,sql,mode,limit in extra:
        add(cid,[turn(text,'按明确商品对象与条件查询，不能扩大成整个装备大类。',sql=sql,mode=mode,limit=limit)])
    add('RF_QUOTE01',[turn('商品989买2件，商品984买3件，合计标价多少？','独立SKU数量2和3，按数据库标价合计2774.95。',
        quote_items=[{'product_id':989,'quantity':2},{'product_id':984,'quantity':3}])])
    add('RF_QUOTE02',[turn('商品989和984各买两件，合并算目录小计。','两个SKU均数量2，合计2209.96。',
        quote_items=[{'product_id':989,'quantity':2},{'product_id':984,'quantity':2}])])
    add('RF_QUOTE03',[
        turn('商品989两件，商品984也要，但我没决定买几件。','保存989数量2、984等待补充，不能猜数量或丢弃已知明细。',clarify=True),
        turn('商品984买三件，合并算小计。','恢复原任务，仅补984数量3，保留989数量2，合计2774.95。',quote_items=[{'product_id':989,'quantity':2},{'product_id':984,'quantity':3}])])
    add('RF_MEMORY01',[
        turn('查看商品989，预算550以内。','保留550预算。',required_product_ids=[989]),
        turn('这预算别改，查同款其他颜色。','银色564.99超过预算，只能说明不符合。',variant_anchor=989,variant_axis='color',variant_ids=[],variant_strict=True)])
    add('RF_SWITCH01',[
        turn('列出50以内的头盔。','头盔范围。',sql='product_subcategory_id=31 AND list_price<=50',mode='subset',limit=5),
        turn('改查整个目录最便宜的五个商品记录，排除零标价。','清除旧头盔与预算范围。',sql='TRUE',mode='ascending',limit=5)])
    return data


def main():
    import evals.broad_runner as engine
    engine.broad_cases=cases;engine.VERSION='reliability-20261103-2'
    return engine.main()


if __name__=='__main__':raise SystemExit(main())
