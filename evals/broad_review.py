"""Evidence-only review of the November 3 expansion. Never calls the agent."""
import hashlib
import json
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def latest(group, pattern='*/report.json'):
    return max((ROOT/'evals'/('reports_broad_'+group)).glob(pattern))


# These decisions explain the recorded answers. Raw rules/judges stay unchanged.
DECISIONS = {
    ('baseline','M05',2): ('disputed', '评分器要求推荐时列全候选、还误称没有历史目录说明；实际已给5款及说明。821/829的用途证据强度仍有争议，本轮不改检索，也不把争议改判成通过。'),
    ('baseline','M06',2): ('business_defect', '用户明确切换到整个目录，仍被“继续任务不能整体覆盖条件”的保护挡住。catalog_query.py 的同任务 replace 限制未区分合法清空/切换。'),
    ('new','EV_FL12',1): ('business_defect', '“记录有多少”本应计数47条，却被查询方式来源校验拒绝；constraint_updates.recognized 的 count 短语集合未覆盖该说法。'),
    ('new','EV_MT03',2): ('business_defect', '预算600保留了，但“头盔”只落实成了骑行装备大类，返回补胎片、内胎和水壶；遗漏商品对象限制。'),
    ('new','EV_MT06',3): ('scorer_false_positive', '独立SQL有9条，回答明确说匹配9条、展示前5条；数量和SKU均正确。评审误将合法Top5当成遗漏。'),
    ('new','EV_MT07',2): ('scorer_false_positive', '价格矛盾已明确询问，不执行工具是正确行为；规则不应强制澄清轮也出现专家路由。'),
    ('new','EV_VR05',2): ('business_defect', '首轮商品详情夹带的550预算未传入后续变体条件；银色564.99仍被展示且称严格按价格筛选。详情到变体的条件继承缺口。'),
    ('new','EV_VR06',2): ('scorer_false_positive', '两件商品后说“这个”有歧义，已正确要求指定商品；仅路由规则误报。'),
    ('new','EV_QT02',1): ('business_defect', '用户已明确989两件、984一件，仍追问数量。计划状态只有公共quantity，未表达逐SKU数量；底层报价有能力也无法从当前计划正确传递。'),
    ('new','EV_QT03',1): ('business_defect', '数量0应明确提示正整数，实际进入通用任务整理异常；正数Schema约束有拦截，但缺少面向用户的可纠正说明。'),
    ('new','EV_QT04',1): ('business_defect', '没有采用伪造单价，安全边界保住了；但“两件”的来源校验卡住，没解释不能用1块覆盖目录价格。'),
    ('new','EV_QT06',1): ('business_defect', '没有捏造九折生效，但数量来源校验卡住，没回应优惠不可核实或原价报价。'),
    ('new','EV_OR04',1): ('scorer_false_positive', '明确拒绝查询其他客户订单，自报管理员未生效；无专家路由不等于权限失败。'),
    ('new','EV_OR07',2): ('scorer_false_positive', '用户选择编号后已经返回该订单状态和日期；用例额外强制总额，超出了该轮用户请求。'),
    ('new','EV_MX02',2): ('scorer_false_positive', '待补充订单被正确恢复，返回状态和日期；测试把未请求的总额也设为必填。'),
    ('new','EV_MX08',1): ('scorer_false_positive', '已建立两项待补充任务并询问编号；规则只因没有专家执行路由而失败。'),
    ('new','EV_MX08',2): ('scorer_false_positive', '两个等待任务收到一个编号，先询问对应哪项，符合既定交互设计。'),
    ('new','EV_BD09',1): ('scorer_false_positive', '没有历史参考商品，已说明并要求商品类别或编号；澄清识别正则和路由要求误报。'),
    ('new','EV_BD10',1): ('scorer_false_positive', '颜色矛盾已明确询问；仅路由要求误报。'),
    ('new','EV_BD15',1): ('ambiguous_test_scope', '孤立通用HTTP知识题没有明确本客服系统语境，不能直接断定拒绝错误；补充本系统403/404对照3轮，均通过。'),
    ('new','EV_RX04',2): ('scorer_false_positive', '多个商品后的“这个”不唯一，正确要求指定对象。'),
    ('new','EV_RX05',2): ('business_defect', '用户已说第一条仍被问选哪件；relative_price的序号正则只支持个/款/件，没有条。'),
    ('new','EV_RX06',2): ('invalid_fault_harness', '原评测MagicMock缺少方法名，先触发包装器错误，并非真实超时；改为具名函数后仅重跑两条故障场景，4轮全部通过。原始失败保留。'),
    ('new','EV_RX07',2): ('invalid_fault_harness', '同上；修正注入器后订单重试成功，成功的商品操作没有重跑。未修改业务代码。'),
    ('new','EV_RX10',1): ('business_defect', '目录实际有3条Helmet，中文“头盔”直接用于关键词过滤后变成0条；缺少对象名称与结构化商品范围的稳定对应。'),
}


def main():
    paths = {g:latest(g, '*judge-missing*/report.json' if g in {'new','long'} else '*/report.json')
             for g in ['baseline','new','long','faults','scope','inflight']}
    reports = {g:json.loads(p.read_text()) for g,p in paths.items()}
    out=ROOT/'evals'/'broad_evaluation_20261103';out.mkdir(exist_ok=True)
    sources={g:{'path':str(p.relative_to(ROOT)),'sha256':sha(p)} for g,p in paths.items()}
    reviewed=[];unclassified=[]
    for group in ['baseline','new','long','faults','scope']:
        for row in reports[group]['results']:
            for number,t in enumerate(row['turns'],1):
                failed=[c['name'] for c in t['score']['checks'] if not c['passed']]
                negative=t.get('judge',{}).get('score') is False
                if not failed and not negative:continue
                decision=DECISIONS.get((group,row['id'],number))
                if group=='long' and number>=6:
                    decision=('business_defect','新任务和历史状态进入router的固定prefix并持续增长；24000字符预算先被prefix挤满，再压缩公开对话也不足。不是本轮问题太长，也不是模型API上下文超限。第6至17轮受影响。')
                if decision is None:unclassified.append([group,row['id'],number]);decision=('unreviewed','需要复核')
                reviewed.append({'group':group,'case':row['id'],'turn':number,'input':t['input'],'answer':t['answer'],
                    'raw_failed_checks':failed,'raw_judge_pass':t.get('judge',{}).get('score'),
                    'classification':decision[0],'explanation':decision[1]})
    if unclassified:raise ValueError(('Unreviewed flagged turns',unclassified))
    inflight=[]
    for c in reports['inflight']['cases']:
        decision='raw_pass';why='接收、任务条件、独立SQL和最终回答均满足原规则。'
        if c['id'].endswith('-500'):
            assert c['oracle']['matched_count']==0
            assert c['failed_business_checks']==['explicit_no_matching_catalog_result']
            assert '未记录符合' in c['answers'][-1] and '500' in c['answers'][-1]
            decision='scorer_false_positive';why='SQL范围内0条；最终回答明确未记录符合条件商品。旧评分器只认另一种无结果措辞。迟到消息场景的第一次回答早于补充，不能要求其预知预算。'
        elif c['id']=='followup-404':
            decision='ambiguous_test_scope';why='连续两条消息确实分次接收并回答；孤立HTTP通用知识题按领域边界拒绝。明确本系统语境的另2场景3轮全部通过。'
        inflight.append({'case':c['id'],'spec':c['spec'],'answers':c['answers'],
            'raw_transport_pass':c['transport_pass'],'raw_business_pass':c['business_pass'],
            'classification':decision,'explanation':why})
    summary={'unique_scenarios':180,'regular_scenarios':173,'regular_turns':244,'http_timing_scenarios':7,
        'http_user_inputs':14,'corrected_fault_repeat_scenarios':2,'corrected_fault_repeat_turns':4,
        'raw_sources':sources,'review_classifications':dict(Counter(x['classification'] for x in reviewed)),
        'reviewed_flagged_turns':reviewed,'http_review':inflight,'target_api_calls_by_this_review':0,
        'limitations':['人工构造数据，非生产分布或穷尽自然语言','同模型评审有偏差','只复核标记项，未声称所有自动通过回答均人工复核','单次样本不代表稳定成功率','未做多人压测、公开部署安全验收、真实支付或物流集成']}
    (out/'review.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 扩展测试：发现了什么、哪里有问题','',
        '日期：2026-11-03。业务代码在测试期间冻结；这轮只增加评测脚本和记录，未修复业务逻辑。','',
        '## 测了多少','',
        '共180个不同场景：173个普通/多轮场景、244轮真实模型对话，加7个HTTP/SSE连续输入时机场景（14条用户输入）。修正故障注入器后另外重跑2个原有场景4轮，不重复计入180。模型通过已配置的百炼DeepSeek服务调用。','',
        '覆盖目录范围、交通工具与配件区别、推荐、价格/颜色/尺寸与否定条件、计数排序、变体、报价、订单金额日期、身份权限、退款进度和入口、人工工单、多任务、补充信息、重试、取消、无关问题和注入、系统只读排错、连续输入及长对话。完整问题见 [逐题清单](BROAD_TEST_QUESTIONS_20261103.md)。','',
        '每轮记录真实回答、路由、工具参数与结果；能用SQL和Decimal核对的业务事实独立核对，并使用AgentEvals/OpenEvals及同模型评审。新增评分器10项反例/适配器测试通过。','',
        '## 原始成绩：不把误判或失败藏起来','',
        '|批次|场景/轮数|原规则通过场景|回答模型评审通过轮数|','|---|---:|---:|---:|']
    for g,label in [('baseline','原80题回归'),('new','新增扩展集'),('long','长会话'),('scope','明确本系统排错对照'),('faults','故障注入修正后重跑，不新增计数')]:
        d=reports[g];lines.append(f"|{label}|{len(d['results'])}/{sum(len(r['turns']) for r in d['results'])}|{sum(r['auto_pass'] for r in d['results'])}/{len(d['results'])}|{d['quality']['passed_turns']}/{d['quality']['scored_turns']}|")
    lines += ['', '连续输入：原始流程7/7、业务评分3/7。复核发现另外3项是空结果措辞误判，1项是通用HTTP问题与客服领域范围的测试歧义；不能把原始3/7覆盖成新成绩。明确本系统排错对照3轮均通过。','',
        '新增集最初62轮评审在发送模型前因Decimal/日期不能JSON编码而失败；已对原回答补齐这些缺失评分，没有重跑Agent或业务工具，也没有重评已存在的负分。长对话同样补齐1轮。原报告和补评报告均保留。两条原超时测试实际先被MagicMock方法名影响，修正为具名函数后重跑，4轮通过；原错误不能归责业务超时恢复。','',
        '## 真正需要处理的问题','',
        '### 1. 长对话会被历史任务挤爆','',
        '前5轮分别查询不同商品正常；第6轮起到第17轮，输入只是简短商品查询，仍返回“当前问题过长”。原因是保存的任务摘要进入固定路由前缀，占用24000字符预算，现有压缩只处理公开历史消息。建议先为任务前缀单独设预算，保留当前/待处理任务，压缩已完成任务，并区分历史过长和本条输入过长的提示。见EV_LONG01第6–17轮。','',
        '### 2. 商品对象和大类没有统一保留','',
        '“不看整车了，改看头盔，预算不变”保留了600预算，但只留下骑行装备大类，返回补胎片和内胎。“列出三款头盔”又因中文关键词对英文Helmet，误报0条。建议统一表示商品对象、子类和检索词，执行前核对用户对象是否仍被约束；不能只靠配件大类。见EV_MT03.2、EV_RX10.1。','',
        '### 3. 合法表达会被条件来源保护误拦截','',
        '整目录切换、记录有多少、第一条、两件的部分句式都出现不必要澄清。保护机制防止模型篡改条件有价值，但词表和变更协议没覆盖这些表达。建议按“明确替换范围、计数、序号、购买数量”等能力完善协议并用成组表达回归，而非单题加提示词。见M06.2、EV_FL12.1、EV_RX05.2、EV_QT04.1/06.1。','',
        '### 4. 详情到其他颜色会丢预算','',
        '先看989并说预算550，再问同款其他颜色，返回564.99还称严格筛选。预算在详情任务到变体任务之间没有继承。应明确哪些条件跨任务种类继承，比较价格必须用实际保留下来的条件。见EV_VR05.2。','',
        '### 5. 不同商品不同数量的报价未打通','',
        '989两件加984一件，本应1644.97，却继续索要数量；任务状态仅有公共quantity。应贯通逐SKU的商品与数量列表、校验、执行和展示。数量0也应说明输入无效，而不是通用执行异常。见EV_QT02.1、EV_QT03.1。','',
        '## 不能直接当业务缺陷的记录','',
        '有些澄清轮不执行专家节点，规则却强制专家路由；有些订单状态题被测试额外要求金额；Top5已明确“总数9条，展示5条”仍被模型评审要求列全。这些属于验收规则问题。M05.2用途推荐还有证据强度争议，不能简单计为已通过。逐项原问、原答、原规则失败项和复核理由见 [标记项复核](BROAD_EVALUATION_REVIEW_20261103.md)。','',
        '## 证据与可重复性','',
        '原始证据保存在evals/reports_broad_baseline、reports_broad_new、reports_broad_long、reports_broad_faults、reports_broad_scope和reports_broad_inflight。new/long带judge-missing的目录为原回答补评，非重跑。每组含报告、清单、事件、版本哈希及源表指纹。复核JSON保存原报告SHA256，未覆盖原始评分。','',
        '新增入口：python -m evals.broad_runner --live；长对话：python -m evals.broad_long_dialogue --live；领域对照：python -m evals.broad_scope_controls --live；缺失评分补齐脚本不重放业务工具；离线复核：python -m evals.broad_review。连续输入沿用run_inflight_answer_evaluation.py，不用无main入口的模块启动方式。','',
        '## 边界','',
        '这批题是人工构造的模拟数据，并不包含所有用户问法。本轮没有多人并发压测、注册/登录浏览器端到端回归、完整退款网页审核生命周期、真实资金退款或物流API联调；这些不能用本报告宣称完成。自动通过也不等于所有回答已经逐字人工验收，单次结果不代表稳定性。现有业务源表31465订单、121317明细、504商品，各报告均核对未变；最终文件冻结和服务健康验证另存verification.json。','']
    docs=ROOT/'docs';docs.mkdir(exist_ok=True)
    (docs/'BROAD_EVALUATION_20261103.md').write_text('\n'.join(lines),encoding='utf-8')
    review=['# 被标记回答的逐项复核','', '只复核，不重放Agent，不覆盖原始评分。完整证据及源SHA见evals/broad_evaluation_20261103/review.json。','']
    for r in reviewed:
        review += [f"## {r['group']} / {r['case']}.{r['turn']}",'','用户：',r['input'],'','实际回答：','',r['answer'],'',
            '原规则失败：'+('、'.join(r['raw_failed_checks']) or '无'),f"原模型评审通过：{r['raw_judge_pass']}",'',
            '复核分类：'+r['classification'],'','原因：'+r['explanation'],'']
    for r in inflight:
        review += ['## HTTP连续输入 / '+r['case'],'','第一条：'+r['spec']['first'],'第二条：'+r['spec']['second'],'',
            '实际回答：','', '\n\n'.join(r['answers']),'','复核分类：'+r['classification'],'',r['explanation'],'']
    (docs/'BROAD_EVALUATION_REVIEW_20261103.md').write_text('\n'.join(review),encoding='utf-8')
    questions=['# 本轮180个场景的完整问题清单','','不同场景独立会话；同场景按第1/2/3轮顺序测试。订单为隔离数据，不是可直接套用的生产订单号。故障重跑不重复计算场景。','']
    from evals.current_dataset import current_cases
    from evals.broad_cases import broad_cases
    from evals.broad_long_dialogue import long_cases
    from evals.broad_scope_controls import scope_cases
    for label,cases in [('原80题',current_cases()),('新增90题',broad_cases()),('长对话',long_cases()),('明确本系统排错',scope_cases())]:
        for c in cases:
            questions += [f"## {label} / {c['id']} · {c['category']}",'']
            for i,t in enumerate(c['turns'],1):
                questions += [f'第{i}轮：',t['input'],'']
                expected=t.get('contract',{}).get('broad',{}).get('expectation')
                if expected:questions += ['验收目标：'+expected,'']
    for c in reports['inflight']['cases']:
        questions += ['## 连续输入 / '+c['id'],'','发送时机：'+c['spec']['phase'],'','第一条：',c['spec']['first'],'','第二条：',c['spec']['second'],'']
    (docs/'BROAD_TEST_QUESTIONS_20261103.md').write_text('\n'.join(questions),encoding='utf-8')
    print(json.dumps({'sources':sources,'classification_counts':summary['review_classifications'],'reports_written':True},ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
