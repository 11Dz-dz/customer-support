"""Optional, request-scoped input checkpoints. CLI/evaluation calls have no inbox."""
from contextlib import contextmanager
from contextvars import ContextVar

from langchain_core.messages import HumanMessage

_inbox = ContextVar('customer_support_turn_inbox', default=None)


class TurnStopped(RuntimeError):
    """Cooperative stop: current model/tool work is allowed to settle first."""


@contextmanager
def inbox_scope(inbox):
    token = _inbox.set(inbox)
    try:
        yield
    finally:
        _inbox.reset(token)


def current_inbox():
    return _inbox.get()


def answer_metadata():
    if current_inbox() is None:
        return {}
    from datetime import datetime,timezone
    return {'web_answer_at':datetime.now(timezone.utc).isoformat()}


def checkpoint_inputs(state, config, *, seal=False):
    inbox = current_inbox()
    if inbox is None:
        return None
    rows = inbox.poll(seal=seal)
    if not rows:
        return None
    from chat_events import emit_event
    emit_event('inflight_input_consumed', config, input_count=len(rows),
               input_ids=[str(r['input_id']) for r in rows], final_boundary=seal)
    return {'messages': [HumanMessage(content=r['message'], id=str(r['input_id'])) for r in rows],
            'inflight_user_ids': list(dict.fromkeys(state.get('inflight_user_ids', []) +
                                      [str(r['input_id']) for r in rows])),
            'inflight_replan': bool(state.get('inflight_active')), 'next': 'REPLAN'}


def merge_inflight_plan(state, tasks, ids, outputs, notice):
    """Keep unaffected, unannounced tasks; discard answers for changed revisions."""
    if not state.get('inflight_replan'):
        return ids, []
    if notice:
        # Never publish a stale answer when the changed requirements could not be planned.
        return [], []
    by_id = {t.task_id: t for t in tasks}
    ids = list(dict.fromkeys([k for k in state.get('turn_task_ids', []) if k in by_id] + ids))
    kept = [o for o in outputs if o['task_id'] in by_id and
            o.get('revision') == by_id[o['task_id']].revision]
    return ids, kept


def inflight_evidence_ids(state,task):
    """Only this task's successful reads after this still-unpublished segment began.

    Earlier completed segments and other tasks are not made current by a replan.
    Candidate scope is still rechecked against the newly planned price/category.
    """
    ids=state.get('inflight_user_ids',[])
    if not ids or not state.get('inflight_rounds'):return set()
    messages=list(state.get('messages',[]))
    start=next((i for i,m in enumerate(messages) if isinstance(m,HumanMessage) and m.id==ids[0]),None)
    if start is None:return set()
    from langchain_core.messages import ToolMessage
    from tool_result import normalize_result
    allowed=set(task.evidence_call_ids)
    return {m.tool_call_id for m in messages[start+1:] if isinstance(m,ToolMessage)
            and m.tool_call_id in allowed and m.name in {'search_products','get_product_info'}
            and normalize_result(m.content,m.name,transport_error=m.status=='error').status=='success'}
