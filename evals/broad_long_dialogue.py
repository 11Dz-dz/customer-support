"""A separate long-session control; imported application code is left unchanged."""
from evals.broad_cases import turn

def long_cases():
    products=[989,984,959,776,749,870,712,711,709,714,716,947,915,821,829,872]
    turns=[turn('新建一项独立查询：查看商品'+str(pid)+'的资料，前面的任务保留。',
        '完成本轮指定SKU的资料查询，不能返回旧商品或通用执行异常。',required_product_ids=[pid]) for pid in products]
    turns.append(turn('按刚才最后展示的那款商品买两件，算不含税和运费的小计。',
        '使用最后实际展示的商品872，数量2，标价小计17.98。',quote_items=[{'product_id':872,'quantity':2}]))
    return [{'id':'EV_LONG01','category':'long_dialogue','family':'EV_LONG01','split':'dev','actor':'owner',
        'smoke':False,'expected_refunds':0,'fixture_refunds':{},'manual_review_required':True,'turns':turns}]

def main():
    import evals.broad_runner as engine
    engine.broad_cases=long_cases;engine.VERSION='broad-long-20261103-1'
    return engine.main()

if __name__=='__main__':raise SystemExit(main())
