"""Grounded refusal review using the turn's shared repair allowance."""
import re
from backend.tasks.constraint_updates import ConstraintConflict

BUSINESS = re.compile(r'卖|有没有|有卖|能买|哪里买|推荐|多少钱|价格|有货|除了.+还有|写着.*钱')
EXCLUDED = re.compile(r'写.{0,6}(?:诗|文案|代码|程序|广告)|(?:编写|生成).{0,8}(?:代码|程序)|翻译', re.I)


def source_fragment(item, inputs, task_count):
    if item.source_index is not None and item.source_text:
        if item.source_index < len(inputs) and item.source_text in inputs[item.source_index]:
            return item.source_text, 'verified_fragment'
        return None, 'invalid_source'
    if task_count == 1 and len(inputs) == 1:
        return inputs[0], 'single_task_fallback'
    return None, 'missing_fragment'


def refusal_review(plan, inputs):
    checks = []
    for item in plan.tasks:
        if item.parameters.conversation_intent != 'out_of_scope':
            continue
        text, reason = source_fragment(item, inputs, len(plan.tasks))
        business = list(BUSINESS.finditer(text or ''))
        excluded = list(EXCLUDED.finditer(text or ''))
        eligible = bool(text and business and not excluded)
        checks.append({'ref': item.ref, 'eligible': eligible,
                       'reason': reason if not text else 'excluded_action' if excluded else 'eligible' if business else 'no_business_action',
                       'business_spans': [list(m.span()) for m in business],
                       'excluded_spans': [list(m.span()) for m in excluded]})
    return checks


def validate_coverage_sources(plan, inputs):
    for item in plan.tasks:
        if item.parameters.operation != 'coverage':
            continue
        text, _ = source_fragment(item, inputs, len(plan.tasks))
        requested = item.parameters.requested_item
        if not text or not requested or requested not in text:
            raise ConstraintConflict('catalog_filter', '覆盖检查对象缺少对应用户原文', repairable=True)
        if item.source_text is None:
            item.source_text=text
            item.source_index=0


def validate_scope_repair(before, after, eligible):
    old = {item.ref: item for item in before.tasks}
    new = {item.ref: item for item in after.tasks}
    if old.keys() != new.keys() or before.cancel_all != after.cancel_all or before.clarification != after.clarification:
        raise ValueError('Scope repair changed unrelated plan structure')
    for ref, item in new.items():
        if ref not in eligible:
            if item.model_dump()!=old[ref].model_dump():
                raise ValueError('Scope repair changed an unrelated task')
        elif not (item.task_type == 'catalog_query' and item.parameters.operation == 'coverage'
                  and not item.continuation and not item.depends_on and not item.cancel and not item.retry_failed
                  or item.task_type == 'conversation' and item.parameters.conversation_intent == 'out_of_scope'):
            raise ValueError('Scope repair chose an unsupported action')
