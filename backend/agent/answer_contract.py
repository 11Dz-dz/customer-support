"""业务回答计划与证据渲染。模型选择内容，关键事实只取自工具证据。"""
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Literal
from backend.agent.prompt_catalog import read_prompt

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from backend.orders.adventureworks_service import DATE_LABELS
from backend.tasks.task_result import collect_evidence, collect_task_result, evidence_pairs
from backend.tasks.task_operations import DENIAL_CODES, operation_labels
from backend.catalog.catalog_filters import category_matches
from backend.catalog.catalog_query import matches as query_matches


class EvidenceQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: StrictInt
    field: Literal["description_zh", "description_en"] = "description_zh"
    text: str = Field(max_length=1200)


class AnswerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["data", "ask_order", "ask_product", "refund_policy", "unknown", "greeting", "no_recommendation"] = "data"
    focus: list[Literal["order", "dates", "amounts", "items", "payment", "invoice", "refund",
                        "catalog", "products", "quote", "inventory", "promotion"]] = Field(default_factory=list, max_length=13)
    product_ids: list[StrictInt] = Field(default_factory=list, max_length=20)
    order_id: StrictInt | None = None
    explanations: list[EvidenceQuote] = Field(default_factory=list, max_length=5)
    recommendation_status: Literal['no_candidates','insufficient_evidence','invalid_selection','retrieval_failed'] | None = None


PLAN_INSTRUCTION = read_prompt("answer_plan")


def money(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("非有限金额")
    return format(number, "f")


def cell(value):
    """源字段只作为文字展示，避免其中的Markdown改变结构。"""
    return str(value).replace("\n", " ").replace("\r", " ").replace("|", "／").replace("`", "＇")


def evidence(messages):
    return evidence_pairs(collect_evidence(messages))


def product_map(results):
    found = {}
    for _, value in results:
        if value.get("success") is not True:
            continue
        records = value.get("products", []) + value.get("items", [])
        if isinstance(value.get("product"), dict):
            records = records + [value["product"]]
        if type(value.get("product_id")) is int:
            records = records + [value]
        for bundle in value.get("bundles", []):
            records += bundle.get("items", [])
        for item in records:
            pid = item.get("product_id")
            if type(pid) is not int:
                continue
            normalized = {**item}
            if "name" not in normalized and "product_name" in normalized:
                normalized["name"] = normalized["product_name"]
            found[pid] = {**found.get(pid, {}), **normalized}
    return found


def _catalog(value):
    filters = value.get("filters", {})
    labels = {"keyword": "关键词", "min_price": "最低标价", "max_price": "最高标价", "color": "颜色", "size": "尺寸"}
    conditions = [f"{labels[key]}={cell(filters[key])}" for key in labels if filters.get(key) is not None]
    if filters.get('catalog_filter'):
        from backend.catalog.catalog_query import describe_filters
        label=describe_filters(filters['catalog_filter'])
        conditions=[label] if label else []
    scope = "全部符合条件的目录商品" if conditions else "全部目录商品"
    parts = ["目录查询范围：" + scope + ("（" + "，".join(conditions) + "）" if conditions else "") + "。",
             f"有效匹配记录：{value['matched_count']}条。"]
    if value.get("statistics"):
        stats = value["statistics"]
        if stats.get("min_price") is not None:
            parts.append(f"最低目录标价：{money(stats['min_price'])}；最高目录标价：{money(stats['max_price'])}；"
                         f"平均标价约：{Decimal(str(stats['average_price'])):.2f}。")
    products = value.get("products", [])
    if products:
        sort = "从低到高" if value.get("sort_order") == "asc" else "从高到低"
        if value.get("sort_by") == "price":
            parts.append(f"在上述完整匹配范围内按标价{sort}展示前{len(products)}条：")
        for product in products:
            parts.append(f"- {cell(product['name'])}（商品{product['product_id']}，{cell(product.get('product_number', ''))}），"
                         f"目录标价：{money(product['list_price'])}；颜色：{cell(product.get('color') or '未提供')}；"
                         f"尺寸：{cell(product.get('size') or '未提供')}。")
        if value.get("extreme_tie_count") is not None:
            label = "最低" if value.get("extreme_kind") == "min" else "最高"
            count = value["extreme_tie_count"]
            parts.append(f"该查询范围的{label}标价为{money(value['extreme_price'])}，对应{count}条商品记录；"
                         + ("该价位没有并列。" if count == 1 else "这些记录在该价位并列。"))
        if value.get("boundary_price_tie_count") is not None:
            parts.append(f"列表末项价位{money(value['boundary_price'])}在完整匹配范围内共有"
                         f"{value['boundary_price_tie_count']}条同价记录。")
    elif value.get("matched_count") == 0:
        parts.append("没有符合这些条件的商品。")
    return "\n".join(parts)


def _quote(value):
    bundles = value.get("bundles")
    if bundles is None:
        bundles = [{"bundle_index": 1, "items": [{"product_id": value["product_id"],
                    "product_name": value["product_name"], "quantity": value["quantity"],
                    "unit_price": value["unit_price"], "line_total": value["subtotal"]}],
                    "subtotal": value["subtotal"], "within_budget": None}]
    parts = []
    for bundle in bundles:
        parts.append(f"方案{bundle['bundle_index']}：")
        recomputed = Decimal(0)
        for item in bundle["items"]:
            total = Decimal(str(item["unit_price"])) * item["quantity"]
            if total != Decimal(str(item["line_total"])):
                raise ValueError("报价明细与单价数量不一致")
            recomputed += total
            parts.append(f"- {cell(item['product_name'])}（商品{item['product_id']}）："
                         f"{money(item['unit_price'])} × {item['quantity']} = {money(total)}")
        if recomputed != Decimal(str(bundle["subtotal"])):
            raise ValueError("报价合计与明细不一致")
        parts.append(f"商品小计：{money(bundle['subtotal'])}。")
        if value.get("budget") is not None:
            inside = recomputed <= Decimal(str(value["budget"]))
            parts.append(f"预算上限：{money(value['budget'])}；" + ("本方案在预算内。" if inside else "本方案超过预算。"))
    parts.append("各方案分别报价，按历史目录标价估算；币种未知，未包含税费、运费及未核实优惠，不创建订单。")
    return "\n".join(parts)


def _refund(value, *, from_history=False):
    status = {"pending": "待人工审核", "approved": "申请已批准（不代表资金已退回）",
              "rejected": "申请未通过", "cancelled": "申请已取消"}.get(value.get("status"), "状态未核实")
    if from_history:
        lead = "依据本会话已有记录，申请已经登记；本轮未重复提交。"
    elif value.get("already_exists"):
        lead = "该订单已有未完成的退款申请，本次复用，没有重复登记。"
    else:
        lead = "已登记退款申请。"
    funds = ("本次工具操作未执行资金退款，登记申请也不表示退款资格已确认。"
             if value.get("funds_action") == "not_executed_by_this_system" else
             "当前没有可核实的资金执行记录，申请状态不代表资金已退回。")
    return (f"{lead}\n订单：{value['order_id']}；申请编号：{cell(value['refund_id'])}；"
            f"{'上次记录的' if from_history else '当前返回的'}状态：{status}。\n"
            f"{funds}\n"
            "当前仅支持登记或复用申请；凭证上传、修改原因和审核结果通知尚未接入。")


def build_business_answer(messages, draft, role):
    """旧调用适配器；新工作流将明确的 TaskResult 直接交给展示层。"""
    last_human = max((i for i, m in enumerate(messages) if getattr(m, "type", None) == "human"), default=-1)
    result = collect_task_result(messages)
    # 仅兼容旧脚本；当前工作流的对象由任务记录提供。
    explicit = re.search(r"订单(?:号)?\s*(?:SO)?(\d+)", str(getattr(messages[last_human], "content", "")), re.I) if last_human >= 0 else None
    result.requested_order_id = int(explicit.group(1)) if explicit else None
    return render_business_result(result, draft, role)


def parse_answer_plan(draft):
    """Use the same JSON/fence handling for selection, task state and presentation."""
    text = draft.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return AnswerPlan.model_validate_json(text)


def render_business_result(result, draft, role):
    """只展示已收集的业务结果；不再从聊天中推断任务或重新抽取订单号。"""
    current, history = evidence_pairs(result.retained+result.current), evidence_pairs(result.history)
    fallback = False
    try:
        plan = parse_answer_plan(draft)
    except (ValueError, TypeError):
        plan, fallback = AnswerPlan(), True
    known = product_map(history + current)
    available_products = {pid:p for pid,p in product_map(current if current else history).items()
                          if category_matches(p,result.product_category) and query_matches(p,result.catalog_filter)}
    if any(pid not in available_products for pid in plan.product_ids):
        plan.product_ids = []
        fallback = True
    focus = set(plan.focus)
    errors = [value for name, value in current if value.get("success") is False
              and name in {"query_order", "query_my_orders", "query_invoice", "check_payment_status", "query_refund_status", "request_refund"}]
    if errors and (not any(v.get('success') is True for _,v in current)
                   or any(v.get('error_code') in DENIAL_CODES for v in errors)):
        code = errors[-1].get("error_code")
        if code == "SESSION_UNBOUND":
            answer = "当前会话尚未绑定客户身份，无法访问订单。请联系管理员新建并绑定客户会话；聊天自报客户编号不能替代身份校验。"
        elif code == "ORDER_NOT_ACCESSIBLE":
            answer = "订单不存在或当前会话无权查看，无法提供订单详情或登记申请。请核对订单号，并通过有权限的客户会话处理。"
        else:
            answer = errors[-1].get('tool_message') or "本次订单操作未成功，暂时无法核实结果。"
        return answer, {"plan_fallback": fallback, "mode": "verified_error"}
    successful = [(name, value) for name, value in current if value.get("success") is True]
    coverages=[v for n,v in successful if n=='query_products' and v.get('operation')=='coverage']
    if role=='sales' and coverages:
        from backend.catalog.catalog_coverage import render_coverage
        return render_coverage(coverages[-1]),{'mode':'catalog_coverage','template_id':coverages[-1]['template_id']}
    overviews=[v for n,v in successful if n=='query_products' and v.get('operation')=='overview']
    if role=='sales' and overviews:
        from backend.catalog.catalog_query import render_overview
        return render_overview(overviews[-1]),{'plan_fallback':fallback,'mode':'catalog_overview'}
    variants=[v for n,v in successful if n=='query_product_variants']
    if role=='sales' and variants:
        from backend.catalog.product_variants import render_variants
        return render_variants(variants[-1]),{'plan_fallback':fallback,'mode':'verified_product_variants'}
    # 有本轮结果时，不回退旧结果覆盖当前失败/空查询。
    source = successful if current else [(n, v) for n, v in history if v.get("success") is True]
    parts = []
    if not successful and current:
        reasons=list(dict.fromkeys(v.get('tool_message') or '本项操作未成功，暂时无法核实结果。' for _,v in current))
        return '\n'.join(reasons), {"plan_fallback": fallback, "mode": "verified_error"}
    if role == "billing":
        listings=[v for n,v in successful if n=='query_my_orders']
        if listings:return render_my_orders(listings[-1]),{'plan_fallback':fallback,'mode':'account_order_candidates'}
        status_reads = [v for n,v in successful if n == 'query_refund_status']
        if status_reads and (focus == {'refund'} or not any(n=='query_order' for n,_ in successful)):
            return render_refund_status(status_reads[-1]), {"plan_fallback":fallback,"mode":"live_refund_status"}
        active_refunds = [e.data for e in result.current if e.tool == 'request_refund'
                          and e.data.get('success') is True and e.data.get('refund_id')]
        if plan.kind == "refund_policy" and not active_refunds:
            return ("当前没有可查询的退款政策文本，无法核实条件、时限或费用规则。"
                    "登记申请仅进入待审核状态，当前未接入资金退款。"), {"plan_fallback": fallback, "mode": "policy_unavailable"}
        if status_reads:
            parts.append(render_refund_status(status_reads[-1]))
        values = [v for n, v in source if n != 'query_refund_status' and v.get("order_id") is not None]
        wanted = result.requested_order_id if result.requested_order_id is not None else plan.order_id
        if active_refunds:
            wanted = active_refunds[-1]["order_id"]
        if wanted is not None:
            values = [v for v in values if v.get("order_id") == wanted]
        if wanted is None and values:
            wanted = values[-1]["order_id"]
            values = [v for v in values if v.get("order_id") == wanted]
        if values:
            if not current:
                parts.append("依据本会话此前取得的工具记录，以下为当时已核实的信息：")
            merged = {}
            for value in values:
                merged.update(value)
            parts.append(f"订单：{merged['order_id']}。")
            order_values = [v for v in values if v.get("status_code") is not None]
            if order_values:
                parts.append("订单状态：" + cell(order_values[-1]["status"]) + "。")
            if not focus or "dates" in focus or "order" in focus:
                for key, label in DATE_LABELS.items():
                    if merged.get(key):
                        parts.append(f"{label}：{cell(str(merged[key])[:10])}。")
            if not focus or "amounts" in focus:
                for key, label in (("subtotal", "商品小计"), ("tax_amount", "税费"), ("freight", "运费")):
                    if merged.get(key) is not None:
                        parts.append(f"{label}：{money(merged[key])}。")
                amount = merged.get("total_due", merged.get("order_total"))
                if amount is not None:
                    parts.append(f"订单应付总额：{money(amount)}。")
            if not focus or "items" in focus:
                if "items" in merged:
                    parts.append(f"商品明细：{merged['item_count']}条，合计{merged['total_quantity']}件。")
                for item in merged.get("items", []):
                    parts.append(f"- {cell(item['product_name'])}（商品{item['product_id']}，{cell(item.get('product_number', ''))}），"
                                 f"数量{item['quantity']}；订单成交单价{money(item['unit_price'])}；明细金额{money(item['line_total'])}。")
            refunds = [v for v in values if v.get("refund_id")]
            # A fresh successful read authorizes this order. An old application is
            # historical evidence only, never a live refund/funds status or a write.
            authorized_now = any(n in {'query_order', 'query_invoice', 'check_payment_status'}
                                 and v.get('order_id') == wanted for n, v in successful)
            if not refunds and 'refund' in focus and authorized_now:
                refunds = [v for n, v in history if n == 'request_refund'
                           and v.get('success') is True and v.get('order_id') == wanted
                           and v.get('refund_id')]
            if refunds and (active_refunds or not focus or "refund" in focus):
                parts.append(_refund(refunds[-1], from_history=not any(
                    e.tool=='request_refund' and e.data.get('refund_id')==refunds[-1]['refund_id'] for e in result.current)))
            if merged.get("payment_status"):
                parts.append("支付状态未知：源数据未提供支付流水或网关状态；订单应付金额不等于已支付金额。")
            if merged.get("invoice_status"):
                parts.append("无法确认发票是否开具：数据源没有独立发票记录，金额摘要不代表发票。")
            parts.append("币种未知；实际签收日期和实时物流轨迹未提供，发货状态不能证明签收或付款。")
        elif plan.kind == "refund_policy":
            parts.append("当前没有可查询的退款政策文本，无法核实条件、时限或费用规则。登记申请仅进入待审核状态，当前未接入资金退款。")
        else:
            parts.append("请提供需要查询的订单号。当前没有与本次请求对应、可确认的订单结果。")
    else:
        if plan.kind=='no_recommendation':
            if plan.recommendation_status=='no_candidates':
                scope=result.catalog_scope
                if scope and scope.get('matched_count')==0:
                    if scope.get('catalog_filter'):
                        from backend.catalog.catalog_query import describe_filters
                        return '当前样例目录未记录符合本次结构化条件'+describe_filters(scope['catalog_filter'])+'的有效标价商品。可调整条件后查询；这不代表实时库存。',{'plan_fallback':fallback,'mode':'verified_empty_catalog'}
                    filters=[]
                    if scope.get('min_price') is not None:filters.append('目录标价不低于'+money(scope['min_price']))
                    if scope.get('max_price') is not None:filters.append('目录标价不超过'+money(scope['max_price']))
                    categories={'bicycles':'自行车整车','components':'自行车零部件','clothing':'骑行服装','accessories':'骑行装备'}
                    for field,label in [('color','颜色'),('size','尺寸')]:
                        if scope.get(field) is not None:filters.append(label+'为'+str(scope[field]))
                    item=categories.get(scope.get('product_category'),'商品')
                    condition='（'+'，'.join(filters)+'）' if filters else ''
                    text='当前样例目录中，没有符合当前价格和类别条件'+condition+'的'+item+'。可以调整预算或商品类别后再查询。'
                    return text+'\n以上为历史样例目录，不代表实时售价或库存，币种未知。',{'plan_fallback':fallback,'mode':'verified_empty_catalog'}
                text=('本次检索未找到符合完整需求的候选；当前价格和类别范围内仍有商品，不能据此断言全目录没有匹配。'
                      if scope else '本次检索未找到符合当前需求的候选；全目录范围尚未核实，无法确认是否有其他匹配。')
                return text,{'plan_fallback':fallback,'mode':'empty_retrieval'}
            if plan.recommendation_status=='retrieval_failed':
                return '商品查询未成功完成，暂时无法核实推荐结果；不能将查询失败解释为没有匹配商品。',{'plan_fallback':fallback,'mode':'retrieval_failed'}
            if plan.recommendation_status=='invalid_selection':
                return '商品选择清单未通过校验，本次未形成可靠推荐；这不表示目录中没有符合条件的商品。',{'plan_fallback':fallback,'mode':'selection_invalid'}
            return ('已检索到候选，但本次未能根据描述确认足够符合需求的推荐。请补充或缩小用途、对象或必需特征；'
                    '不会将未经确认的候选直接作为推荐。'),{'plan_fallback':fallback,'mode':'recommendation_unconfirmed'}
        quotes = [v for n, v in successful if n == "calculate_price" and (v.get("bundles") is not None or v.get("quantity") is not None)]
        catalogs = [v for n, v in source if n == "query_products"]
        if quotes:
            # 同轮多个独立报价都是任务结果，不能只保留最后一次调用。
            seen_quotes = set()
            for quote in quotes:
                signature = json.dumps(quote, sort_keys=True, ensure_ascii=False)
                if signature not in seen_quotes:
                    parts.append(_quote(quote))
                    seen_quotes.add(signature)
        elif catalogs:
            seen = set()
            for value in catalogs:
                signature = json.dumps({k: value.get(k) for k in ("filters", "operation", "sort_by", "sort_order", "limit")}, sort_keys=True)
                if signature not in seen:
                    parts.append(_catalog(value))
                    seen.add(signature)
        current_products = {pid:p for pid,p in product_map(source).items() if category_matches(p,result.product_category) and query_matches(p,result.catalog_filter)}
        selected = plan.product_ids or list(current_products)[:10]
        shown = []
        if not quotes and (not catalogs or "products" in focus):
            for pid in selected:
                product = known.get(pid)
                if not product:
                    continue
                shown.append(pid)
                section = [f"商品{pid}：{cell(product.get('name', pid))}。"]
                for key, label in (("product_number", "型号"), ("color", "颜色"), ("size", "尺寸")):
                    if product.get(key) is not None:
                        section.append(f"{label}：{cell(product[key])}。")
                if product.get("list_price") is not None:
                    section.append("历史目录标价：" + money(product["list_price"]) + "。")
                if product.get("weight") is not None:
                    section.append(f"目录重量：{cell(product['weight'])} {cell(product.get('weight_unit') or '')}。")
                description = product.get("description_zh") or product.get("description_en")
                if description:
                    section.append("目录描述原文：“" + cell(description) + "”")
                parts.append("\n".join(section))
        for explanation in plan.explanations:
            product = known.get(explanation.product_id, {})
            original = product.get(explanation.field) or ""
            if explanation.product_id in selected and explanation.text.strip() and explanation.text in original:
                text = cell(explanation.text)
                if not any(text in section for section in parts):
                    parts.append(f"商品{explanation.product_id}的目录依据：“{text}”")
        if "inventory" in focus:
            parts.append("当前未导入库存，无法确认库存数量或当前是否在售。")
        if "promotion" in focus or any(v.get("promotion_status") for _, v in source):
            parts.append("优惠状态未知：当前没有可核实的优惠活动数据，不能判断有无折扣。")
        if not parts:
            empty_messages=list(dict.fromkeys(v.get('tool_message') for _,v in source if v.get('tool_status')=='empty' and v.get('tool_message')))
            parts.extend(empty_messages or ["暂无可确认的商品或报价结果，请提供商品编号、名称或具体需求。"])
        if source:
            parts.append("以上为AdventureWorks历史样例目录信息，非实时售价；币种、当前库存及在售状态未知。")
    if result.is_retry and result.retained:
        parts.insert(0,'本轮只重试了失败的只读查询；其余内容沿用本任务此前已核实的记录。')
    pending=[o for o in result.operations if o.status!='succeeded']
    if result.status=='partial':
        labels=operation_labels(pending) or '部分查询'
        parts.append('本项仅部分完成，未完成：'+labels+'。'+
                     ('可要求重试失败的查询，成功部分会保留。' if any(o.retryable for o in pending)
                      else '这些操作不会自动重试，请核对条件后明确提出后续请求。'))
    return "\n\n".join(parts), {"plan_fallback": fallback, "mode": "partial" if result.status=='partial' else "evidence_rendered"}


def verified_chunks(text, limit=240):
    """分段输出已经校验的最终正文，拼接必须与持久化内容逐字一致。"""
    for offset in range(0, len(text), limit):
        yield text[offset:offset+limit]


def render_refund_status(value):
    from backend.orders.refund_service import STATUS_LABELS,FUNDS_NOTE
    parts=[f"订单：{value['order_id']}。"]
    if not value['refunds']:
        parts.append('当前没有查询到退款申请。需要申请时请到网页「我的订单」中确认提交。')
    for row in value['refunds']:
        parts.append(f"申请编号：{cell(row['refund_id'])}；状态：{STATUS_LABELS.get(row['status'],'未知')}。")
        parts.append(f"申请原因：{cell(row['reason'])}。")
        if row.get('review_note'):parts.append(f"审核说明：{cell(row['review_note'])}。")
    parts.append(FUNDS_NOTE)
    return '\n'.join(parts)


def render_my_orders(value):
    filtered=value.get('refund_filter') is not None
    count=value['matched_count'];rows=value['orders']
    if not rows:
        return ('当前账号下没有查询到符合该退款状态的订单。可以在「我的订单」中查看其他订单。' if filtered
                else '当前账号下没有查询到订单，请联系管理员核对账号的客户绑定。')
    parts=[f"在当前账号下查到{count}笔"+('符合查询状态的订单。' if filtered else '订单。')]
    for i,row in enumerate(rows,1):
        parts.append(f"{i}. 订单{row['order_number']}（订单号{row['order_id']}）；履约状态：{cell(row['fulfillment_status'])}。")
        parts.append(f"订单应付总额：{money(row['total_due'])}（币种未知，不代表已支付金额）。")
        if row.get('refund_id'):
            parts.append(f"退款申请：{cell(row['refund_id'])}；{cell(row['refund_status_label'])}。")
            if row.get('reason'):parts.append('申请原因：'+cell(row['reason'])+'。')
            if row.get('review_note'):parts.append('审核说明：'+cell(row['review_note'])+'。')
        elif not filtered:parts.append('当前没有退款申请记录。')
    if value.get('has_more'):parts.append(f"本次仅展示最近{len(rows)}笔，可要求最多20笔或到「我的订单」查看。")
    if count>1:parts.append('请回复要继续查询的订单号，我再为你查询该笔订单。')
    else:parts.append('已定位到这笔订单，可以继续询问它的详情或退款进度。')
    if filtered or any(r.get('refund_id') for r in rows):parts.append(value['funds_note'])
    return '\n'.join(parts)
