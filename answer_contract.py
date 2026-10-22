"""业务回答计划与证据渲染。模型选择内容，关键事实只取自工具证据。"""
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from adventureworks_service import DATE_LABELS
from task_result import collect_evidence, collect_task_result, evidence_pairs
from task_operations import DENIAL_CODES, operation_labels
from catalog_filters import category_matches


class EvidenceQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: StrictInt
    field: Literal["description_zh", "description_en"] = "description_zh"
    text: str = Field(max_length=1200)


class AnswerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["data", "ask_order", "ask_product", "refund_policy", "unknown", "greeting"] = "data"
    focus: list[Literal["order", "dates", "amounts", "items", "payment", "invoice", "refund",
                        "catalog", "products", "quote", "inventory", "promotion"]] = Field(default_factory=list, max_length=13)
    product_ids: list[StrictInt] = Field(default_factory=list, max_length=20)
    order_id: StrictInt | None = None
    explanations: list[EvidenceQuote] = Field(default_factory=list, max_length=5)


PLAN_INSTRUCTION = """
工具结果采用schema_version=1格式：外层status是执行结果，业务事实在data内；data.status才是订单/申请/健康等业务状态。
empty表示没有匹配数据，不是临时故障；invalid_argument需修改参数，forbidden不可绕过，只有temporary_error且retryable=true的只读操作可有限重试。业务写入不可自行重试。
最终回答由程序核对证据并渲染。工具选择和调用方式保持正常；结束工具推理时，最后一条回答必须只返回一个JSON回答计划，不要输出自然语言草稿：
{"kind":"data","focus":["products"],"product_ids":[776],"order_id":null,"explanations":[]}
kind只能为data/ask_order/ask_product/refund_policy/unknown/greeting。
focus只能从order,dates,amounts,items,payment,invoice,refund,catalog,products,quote,inventory,promotion选择。
product_ids和order_id必须来自本轮或授权历史工具结果。focus按用户请求选择，例如订单状态用order，订单金额用amounts，推荐用products，组合报价用quote。
解释可放explanations，每项为{"product_id":776,"field":"description_zh","text":"工具描述中的逐字原文"}；只能逐字引用description_zh或description_en，不得添加没有证据的性能、金额、日期、库存或承诺。
缺订单号用ask_order，咨询退款政策且没有政策文本用refund_policy。工具失败也返回data，程序会展示真实错误，不要绕过错误复用旧订单。
涉及多商品方案时先调用calculate_price的bundles模式取得每组真实合计，不自行填写组合价格。订单成交价和商品目录价分别使用各自证据。
"""


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
                          if category_matches(p,result.product_category)}
    if any(pid not in available_products for pid in plan.product_ids):
        plan.product_ids = []
        fallback = True
    focus = set(plan.focus)
    errors = [value for name, value in current if value.get("success") is False
              and name in {"query_order", "query_invoice", "check_payment_status", "request_refund"}]
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
    # 有本轮结果时，不回退旧结果覆盖当前失败/空查询。
    source = successful if current else [(n, v) for n, v in history if v.get("success") is True]
    parts = []
    if not successful and current:
        reasons=list(dict.fromkeys(v.get('tool_message') or '本项操作未成功，暂时无法核实结果。' for _,v in current))
        return '\n'.join(reasons), {"plan_fallback": fallback, "mode": "verified_error"}
    if role == "billing":
        active_refunds = [e.data for e in result.current if e.tool == 'request_refund'
                          and e.data.get('success') is True and e.data.get('refund_id')]
        if plan.kind == "refund_policy" and not active_refunds:
            return ("当前没有可查询的退款政策文本，无法核实条件、时限或费用规则。"
                    "登记申请仅进入待审核状态，当前未接入资金退款。"), {"plan_fallback": fallback, "mode": "policy_unavailable"}
        values = [v for _, v in source if v.get("order_id") is not None]
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
        current_products = {pid:p for pid,p in product_map(source).items() if category_matches(p,result.product_category)}
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
