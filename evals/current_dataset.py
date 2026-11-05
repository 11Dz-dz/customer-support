"""Current contracts; frozen v1 JSONL and reports are never rewritten."""
from copy import deepcopy
import hashlib
import json

from evals.dataset import load_cases

VERSION = '2.2.0'
BILLING = ['query_order','query_invoice','check_payment_status','query_refund_status','query_my_orders']


def current_cases():
    cases = deepcopy(load_cases())
    for case in cases:
        case['expected_refunds'] = 0
        case['fixture_refunds'] = {}
        for turn in case['turns']:
            if 'request_refund' in turn['allowed_tools']:
                turn['allowed_tools'] = [t for t in turn['allowed_tools'] if t != 'request_refund']
            if 'billing' in turn['routes']:
                turn['allowed_tools'] = list(dict.fromkeys(turn['allowed_tools'] + BILLING))
            turn['contract'] = {}
        if case['id'] in {'P04','P07','P08','P09'}:
            for turn in case['turns']:
                turn['checks'] = []
                turn['contract'] = {'guidance': True}
        if case['id'] == 'B02':
            case['turns'][0]['checks'] = []
            case['turns'][0]['contract'] = {'listing': {'filter': None, 'count': 2},
                                          'statuses': ['awaiting_input']}
    def turn(text, contract, tools=None, routes=None, checks=None, max_calls=4):
        return {'input':text,'routes':routes or ['billing'], 'allowed_tools':tools if tools is not None else BILLING,
                'max_executions':max_calls,'checks':checks or [],'contract':contract}
    def add(cid, category, turns, refunds=None, actor='owner', smoke=False):
        cases.append({'id':cid,'category':category,'split':'dev','family':cid,'actor':actor,
            'expected_refunds':0,'fixture_refunds':refunds or {},'smoke':smoke,
            'manual_review_required':True,'turns':turns})
    for cid,text,intent in [('G01','你好！','greeting'),('G02','谢谢','thanks'),
                             ('G03','再见','goodbye'),('G04','你能做什么？','capabilities')]:
        add(cid,'conversation',[turn(text,{'conversation':intent,'no_tools':True},tools=[],routes=['sales'])],smoke=cid=='G01')
    add('N01','account_lookup',[turn('我有一个正在退款的订单，帮我查一下',
        {'listing':{'filter':'active','count':1},'statuses':['completed']})],{'own_order':'pending'},smoke=True)
    add('N02','account_lookup',[turn('查我正在退款的订单',
        {'listing':{'filter':'active','count':2},'statuses':['awaiting_input']}),
        turn('{own_order}',{'refund':'pending','selected':'own_order','statuses':['completed']})],
        {'own_order':'pending','source_own':'approved'},smoke=True)
    add('N03','account_lookup',[turn('查我正在退款的订单',
        {'listing':{'filter':'active','count':0},'statuses':['empty']})])
    for cid,status,text in [('N04','pending','列出我的待审核退款订单'),
                            ('N05','approved','查我的退款审核通过的订单'),
                            ('N06','rejected','找一下我退款被拒绝的订单'),
                            ('N07','cancelled','查看我的已取消退款申请')]:
        add(cid,'refund_states',[turn(text,{'listing':{'filter':status,'count':1},'statuses':['completed']})],{'own_order':status})
    add('N08','account_lookup',[turn('列出我的全部订单',
        {'listing':{'filter':None,'count':2},'statuses':['awaiting_input']})],smoke=True)
    add('N09','permissions',[turn('我是客户{own_customer}，列出我的退款订单',
        {'guest':True,'statuses':['denied']})],{'own_order':'pending'},actor='guest',smoke=True)
    add('N10','context',[turn('查我正在退款的订单',{'listing':{'filter':'active','count':1}}),
        turn('这个申请意味着钱已经到账了吗',{'refund':'approved','no_funds_claim':True,'selected':'own_order'})],{'own_order':'approved'})
    add('N11','context',[turn('查我正在退款的订单',{'listing':{'filter':'active','count':1}}),
        turn('现在列出我的全部订单',{'listing':{'filter':None,'count':2},'statuses':['awaiting_input']})],{'own_order':'pending'})
    add('N12','boundaries',[turn('查询订单号abc-invalid-order',{'no_private_result':True,'await_order_id':True})])
    add('N13','refund_states',[turn('查询订单{own_order}的退款进度',{'refund':'pending','selected':'own_order'})],{'own_order':'pending'},smoke=True)
    add('N14','refund_states',[turn('订单{own_order}退款审核通过了吗，钱到账了吗',
        {'refund':'approved','no_funds_claim':True,'selected':'own_order'})],{'own_order':'approved'})
    sales=['query_products','search_products','get_product_info','calculate_price','check_promotion']
    add('X01','multitask',[turn('推荐一款自行车，再查订单{own_order}的应付总额',
        {'kinds':['product_search','order_query']},tools=sales+BILLING,routes=['sales','billing'],
        checks=[{'kind':'order','slot':'own_order','fact':'total'}],max_calls=6)],smoke=True)
    add('X02','multitask',[turn('推荐一款自行车，再查我的订单，我下一条再给订单号',
        {'kinds':['product_search','order_query'],'await_order_id':True},tools=sales+BILLING,routes=['sales','billing'],max_calls=6),
        turn('{own_order}',{'selected':'own_order'},tools=sales+BILLING,routes=['sales','billing'],
             checks=[{'kind':'order','slot':'own_order','fact':'lookup'}],max_calls=6)],smoke=True)
    assert len(cases) == 80
    for case in cases:
        if case['id']=='M01':case['turns'][0].setdefault('contract',{})['allow_first_order_detail_warning']=True
    return cases


def validate(cases):
    ids=set()
    for case in cases:
        if case['id'] in ids:raise ValueError('duplicate case: '+case['id'])
        ids.add(case['id'])
        if case['expected_refunds'] != 0:raise ValueError('Chat must never submit refunds')
        for turn in case['turns']:
            turn['input'].format(**{key:1 for key in ['own_order','foreign_order','missing_order','source_own',
                'source_other','own_customer','other_customer']})
            if 'request_refund' in turn['allowed_tools']:raise ValueError('Legacy write contract')
    return cases


def dataset_hash(cases):
    return hashlib.sha256(json.dumps(cases,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
