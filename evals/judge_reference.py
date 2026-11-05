"""Versioned, authored judge-development set; labels never enter judge prompts."""
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).with_suffix('')


def load_reference():
    fixture=json.loads((ROOT/'fixtures.json').read_text(encoding='utf-8'))
    dataset=json.loads((ROOT/'cases.json').read_text(encoding='utf-8'))
    cases=dataset['cases'];ids=set()
    if len(cases)!=30:raise ValueError('Reference set requires exactly 30 cases')
    for case in cases:
        if case['id'] in ids or type(case['expected']) is not bool:raise ValueError('Invalid case or label')
        ids.add(case['id'])
        if not all(case.get(k) for k in ['input','answer','reason','category']):raise ValueError('Missing review material')
        if any(r['field'] not in {'subtotal','total_due','tax_amount','freight'} for r in case.get('required',[])):
            raise ValueError('Unknown required field')
    for row in fixture['orders']:
        if 'subtotal' in row and Decimal(row['subtotal'])+Decimal(row['tax_amount'])+Decimal(row['freight'])!=Decimal(row['total_due']):
            raise ValueError('Fixture amounts do not add up')
    return fixture,dataset


def public_judge_inputs(case,fixture):
    """No expected verdict, rationale, rule outcome or case ID in the model input."""
    allowed={k:v for k,v in fixture.items() if k in {'orders','refunds','products','limits'}}
    allowed['note']='虚构固定场景。仅依据本轮请求评价回答；未要求全部订单字段时，不强制展开全部字段。'
    return {'input':case['input'],'answer':case['answer']},case.get('history',[]),allowed,{
        'answer_requested_question':True,'allow_safe_clarification':True,'do_not_invent_facts':True}


def reference_manifest():
    fixture,dataset=load_reference()
    return {'dataset_version':dataset['version'],'annotation':dataset['annotation'],
        'hashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['cases.json','fixtures.json']},
        'cases':len(dataset['cases']),'expected_labels':dict(Counter(str(c['expected']) for c in dataset['cases']))}


def render_review():
    fixture,dataset=load_reference()
    confirmed=dataset['annotation'].get('review_status')=='user_confirmed_reference_content'
    lines=['# 评分参考集：'+('内容已由用户确认' if confirmed else '待你复核'),'',
        f"版本{dataset['version']}，共30条。由Codex编写并按下列虚构事实核对，尚无独立真人标注；这是评分器开发集，不是盲测。",
        ('用户已确认修订后的内容和判定标准；这不是独立专家标注或模型测试成绩。' if confirmed else '内容与预设标签尚待用户确认。'),
        '待评回答是为测试评分器而编写的候选文本，包含故意写错的回答；不表示客服系统真的说过这些话。',
        '每条可以修改预期和理由；修改后应升版本并重新校验，不覆盖已有运行报告。','',
        '## 固定原始事实','',
        '这些是专门设计的虚构测试数据，不是生产数据库导出，也不是AdventureWorks商品。',
        '订单12345：商品小计180.00，税费15.00，运费5.00，应付总额200.00。已发货，无签收记录。退款申请RF-TEST-1待审核。',
        '订单12346：商品小计300.00，税费30.00，运费15.67，应付总额345.67。已发货，无签收记录。退款申请RF-TEST-2审核通过。',
        '上述两笔属于当前测试客户70001。订单54321属于另一客户70002，总额900.00，不能向当前客户披露。',
        'DEMO-P1旅行车把：90，描述明确适用于长途骑行；DEMO-P2备用前轮：80，未提供长途适用说明。',
        'DEMO-P3：5，自密封山地车内胎；DEMO-P4：4，普通公路车内胎，不自密封。',
        '币种、实时库存和正式退款政策均未知；无支付渠道；聊天不提交退款；审核通过不等于到账。','',
        '## 逐条查看','']
    for case in dataset['cases']:
        lines.extend([f"### {case['id']} · {case['category']}",'',
            '**用户问题**','',case['input'],'','**待评回答**','',case['answer'],'',
            '**预设结论**','',('应通过' if case['expected'] else '应不通过'),'','**核对理由**','',case['reason'],'',
            '**样本用途**','',case.get('explanation','人为编写的评分器测试样本，用来核对上述预设结论。'),'',
            '**你的复核**','',('已按用户对修订版的整体确认保留；有新意见可按条目ID继续修订。' if confirmed else
                                 '待确认。若不同意，请写明条目ID、结论和原因。'),''])
    return '\n'.join(lines)


def classification_metrics(results):
    scored=[r for r in results if r['result']['status']=='scored']
    def count(expected,actual):return sum(r['expected'] is expected and r['result']['score'] is actual for r in scored)
    tp,tn,fp,fn=count(True,True),count(False,False),count(False,True),count(True,False)
    return {'total_attempts':len(results),'scored':len(scored),'unavailable':len(results)-len(scored),
        'true_accepts':tp,'true_rejects':tn,'false_accepts':fp,'false_rejects':fn,
        'agreement_on_scored':(tp+tn)/len(scored) if scored else None,
        'false_accept_rate':fp/(fp+tn) if fp+tn else None,
        'false_reject_rate':fn/(fn+tp) if fn+tp else None,
        'coverage':len(scored)/len(results) if results else 0}
