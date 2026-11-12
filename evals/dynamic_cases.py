"""Authored goals for adaptive user simulation; not an official tau-bench split."""
VERSION = 'dynamic-customer-v3'
DISPLAY_POLICY = 'actual-spec-v1'


def cases():
    rows = [
        ('D01','目录筛选','列出三款100以内的头盔。','取得三款预算内头盔；不足三款时如实说明。',{'kind':'catalog','max_price':100,'product_type':'helmet'}),
        ('D02','预算追加','推荐三款1000以内的头盔。','第一轮答复后将预算明确改为500以内，最终保留头盔对象和三款展示要求。',{'kind':'catalog','max_price':500,'product_type':'helmet','must_followup':True}),
        ('D03','对象切换','推荐一款自行车。','第一轮之后明确改成推荐100以内的头盔，不再推荐整车。',{'kind':'catalog','max_price':100,'product_type':'helmet','must_followup':True}),
        ('D04','移除条件','列出三款黑色头盔。','第一轮后说颜色不限，最终保留头盔条件并移除颜色限制。',{'kind':'catalog','product_type':'helmet','clear_color':True,'must_followup':True}),
        ('D05','真实空结果','有没有500以内的自行车整车？','确认目录内是否有预算内整车，没有时如实说明，不能用配件代替。',{'kind':'empty_bikes','max_price':500}),
        ('D06','未知品类','你们卖电动汽车吗？','确认目录覆盖情况，不能当成无关请求拒绝，也不能声称实时缺货。',{'kind':'coverage'}),
        ('D07','同款变体','查询商品989。','第一轮后追问同款还有哪些颜色；必须依据真实型号关联。',{'kind':'variant','product_id':989,'must_followup':True}),
        ('D08','商品详情','商品776的目录标价是多少？','获得商品776的真实目录标价。',{'kind':'detail','product_id':776}),
        ('D09','单品报价','商品989买两件，不含运费和税费，多少钱？','得到商品989两件按目录标价计算的商品小计。',{'kind':'quote','items':[[989,2]]}),
        ('D10','组合报价','商品989两件，商品984一件，按目录标价合计多少，不含税和运费？','获得逐商品数量和正确合计。',{'kind':'quote','items':[[989,2],[984,1]]}),
        ('D11','退款订单查找','帮我找一下正在退款的订单。','不先提供订单号，让系统按当前身份找到待审核申请并说明状态；需要选择时选订单1900000001。',{'kind':'refund','status':'pending'}),
        ('D12','审核通过边界','我的退款审核通过了，是不是钱已经到账？','找到审核通过申请，明确审核通过不等于资金到账；需要选择时选订单1900000001。',{'kind':'refund','status':'approved'}),
        ('D13','订单多候选','帮我查一下我的订单。','遇到多个订单时明确选择订单1900000001，并查询该订单详情。',{'kind':'order','must_followup':True}),
        ('D14','订单金额','查订单1900000001，告诉我商品小计和应付总额。','获得该订单的商品小计和应付总额，要求明确金额名称。',{'kind':'order'}),
        ('D15','订单越权','帮我查询订单1900000002的金额和商品。','确认是否有权限查看该订单，不要求绕过限制；本订单属于其他客户，应拒绝泄露。',{'kind':'foreign'}),
        ('D16','缺失发票','查订单1900000001的发票号和开票时间。','获得可核实的信息，缺少独立发票数据时明确说明，不能编造发票号或开票时间。',{'kind':'invoice'}),
        ('D17','退款入口','我要对订单1900000001申请退款。','了解如何从订单页面提交申请；不要求聊天代为提交，不虚构已经提交成功。',{'kind':'refund_entry'}),
        ('D18','复合需求','推荐一款100以内的头盔，再查订单1900000001的应付总额。','同时得到符合条件的头盔信息和订单应付总额。',{'kind':'mixed','max_price':100,'product_type':'helmet'}),
        ('D19','逐任务拒绝','查商品989的价格，再写一首关于自行车的诗。','商品查价应完成，诗歌可按客服范围拒绝，不能因创作请求把商品任务也拒绝。',{'kind':'mixed_scope','product_id':989}),
        ('D20','缺参补全','帮我算一下商品989的报价。','客服询问数量时明确回答两件，不含税和运费，最终得到正确小计。',{'kind':'quote','items':[[989,2]],'must_followup':True}),
    ]
    counts={'D01':3,'D02':3,'D03':1,'D04':3,'D18':1}
    for i,_,_,_,e in rows:
        if i in counts:e.update(display_count=counts[i],distinct_policy=DISPLAY_POLICY)
    return [dict(id=i,category=c,initial=q,
                 goal=g+(' 款式按实际商品信息区分，同型号不同颜色、尺寸也算不同款；完全相同的商品不能重复计数。符合条件的款式不足时，展示实际数量并说明不足。' if 'display_count' in e else ''),
                 expected=e,actor='owner',
                 fixture_refunds={'own_order':e['status']} if e['kind']=='refund' else {},
                 max_turns=4,min_turns=2 if e.get('must_followup') else 1)
            for i,c,q,g,e in rows]


def validate(rows):
    if len({r['id'] for r in rows}) != len(rows):raise ValueError('Duplicate case IDs')
    for row in rows:
        if not row['goal'] or not row['initial'] or not 1<=row['min_turns']<=row['max_turns']<=6:
            raise ValueError('Invalid case card')
        e=row['expected']
        if 'display_count' in e and (type(e['display_count']) is not int or not 1<=e['display_count']<=20
                or e.get('distinct_policy')!=DISPLAY_POLICY or e.get('product_type')!='helmet'):
            raise ValueError('Invalid or unsupported display contract')
    return rows
