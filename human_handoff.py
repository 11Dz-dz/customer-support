"""Persistent human review queue. Local administrator commands are the only mutation endpoint."""
import json
import threading
import time
from uuid import uuid4

from postgres_config import connect_database
from request_deadline import deadline_scope
from session_identity import validate_session_state

_schema_lock = threading.Lock()
_schema_ready = False
REASONS = {'user_requested': '用户要求人工处理', 'uncertain_write': '申请提交结果需要核对',
           'persistent_failure': '只读操作持续失败'}
STATUS_LABELS = {'pending': '待人工处理', 'in_progress': '人工处理中', 'resolved': '已标记处理完成'}


def ensure_handoff_schema():
    global _schema_ready
    with _schema_lock:
        if _schema_ready:
            return
        with connect_database() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(48151337)')
            conn.execute("""CREATE TABLE IF NOT EXISTS customer_service.human_handoffs (
                ticket_id text PRIMARY KEY, thread_id text NOT NULL, customer_id integer,
                task_id text, reason text NOT NULL CHECK(reason IN ('user_requested','uncertain_write','persistent_failure')),
                subject_key text NOT NULL, status text NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','in_progress','resolved')),
                snapshot jsonb NOT NULL, assigned_to text, resolution_note text,
                version integer NOT NULL DEFAULT 1,
                created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
            )""")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS human_handoffs_active_subject
                ON customer_service.human_handoffs(thread_id,reason,subject_key)
                WHERE status IN ('pending','in_progress')""")
            conn.execute("""CREATE TABLE IF NOT EXISTS customer_service.human_handoff_events (
                event_id bigserial PRIMARY KEY, ticket_id text NOT NULL REFERENCES
                customer_service.human_handoffs(ticket_id) ON DELETE CASCADE,
                event text NOT NULL, actor text NOT NULL, note text,
                created_at timestamptz NOT NULL DEFAULT now()
            )""")
        _schema_ready = True


def escalation_reason(task):
    if any(o.tool == 'request_refund' and o.requires_review for o in task.operations):
        return 'uncertain_write'
    if task.status in {'cancelled', 'denied', 'awaiting_input', 'awaiting_dependency'}:
        return None
    if task.kind == 'human_handoff':
        return 'user_requested'
    # Unknown payment/invoice data, empty results, missing parameters and denials are not incidents.
    for operation in task.operations:
        if (operation.tool == 'request_refund' and operation.status == 'failed'
                and operation.outcome in {'temporary_error', 'error'}
                and operation.error_code not in {'WRITE_RETRY_NOT_ALLOWED', 'TOOL_CALL_LIMIT',
                                                 'REQUEST_TOOL_LIMIT', 'TURN_DEADLINE'}):
            return 'uncertain_write'
        if (operation.tool != 'request_refund' and operation.status == 'failed'
                and operation.attempts >= 2 and operation.outcome in {'temporary_error', 'error'}):
            return 'persistent_failure'
    return None


def _public(row):
    return {key: row[key] for key in ('ticket_id', 'status', 'reason', 'created_at', 'updated_at')}


def create_handoff(config, state, task, reason):
    """Validate trusted session ownership before creating/reusing an active ticket.

    A short DB-only finalization is allowed after the execution deadline. No model,
    order query or refund submission is run in this scope.
    """
    if reason not in REASONS:
        raise ValueError('invalid handoff reason')
    thread_id = config['configurable']['thread_id']
    with deadline_scope(time.monotonic() + 5):
        customer_id = validate_session_state(thread_id, state)
        ensure_handoff_schema()
        subject = 'explicit' if reason == 'user_requested' else task.task_id
        snapshot = {'task_kind': task.kind, 'task_status': task.status, 'failure_code': task.failure_code,
                    'operations': [{'tool': o.tool, 'status': o.status, 'error_code': o.error_code,
                                    'attempts': o.attempts, 'requires_review':o.requires_review}
                                   for o in task.operations]}
        with connect_database() as conn:
            row = conn.execute("""INSERT INTO customer_service.human_handoffs
                (ticket_id,thread_id,customer_id,task_id,reason,subject_key,snapshot)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (thread_id,reason,subject_key) WHERE status IN ('pending','in_progress')
                DO NOTHING RETURNING *""", ('H-' + uuid4().hex.upper(), thread_id, customer_id,
                    task.task_id, reason, subject, json.dumps(snapshot))).fetchone()
            created = row is not None
            if created:
                conn.execute("""INSERT INTO customer_service.human_handoff_events(ticket_id,event,actor)
                    VALUES (%s,'created','system')""", (row['ticket_id'],))
            else:
                row = conn.execute("""SELECT * FROM customer_service.human_handoffs WHERE thread_id=%s
                    AND reason=%s AND subject_key=%s AND status IN ('pending','in_progress') FOR UPDATE""",
                    (thread_id, reason, subject)).fetchone()
                if row is None:
                    raise RuntimeError('ticket changed during creation; retry safely')
                if row['customer_id'] != customer_id:
                    raise RuntimeError('ticket identity mismatch')
                row = conn.execute('UPDATE customer_service.human_handoffs SET snapshot=%s::jsonb,updated_at=now() '
                             'WHERE ticket_id=%s RETURNING *', (json.dumps(snapshot), row['ticket_id'])).fetchone()
        from chat_events import emit_event
        emit_event('handoff_created' if created else 'handoff_reused', config,
                   ticket_id=row['ticket_id'], reason=reason, status=row['status'])
        return _public(row)


def session_handoffs(config, state):
    thread_id = config['configurable']['thread_id']
    with deadline_scope(time.monotonic() + 5):
        customer = validate_session_state(thread_id, state)
        ensure_handoff_schema()
        with connect_database() as conn:
            rows = conn.execute("""SELECT * FROM customer_service.human_handoffs WHERE thread_id=%s
                AND customer_id IS NOT DISTINCT FROM %s ORDER BY created_at DESC LIMIT 5""",
                (thread_id, customer)).fetchall()
    return [_public(row) for row in rows]


def ticket_text(ticket):
    return (f"工单编号：{ticket['ticket_id']}\n状态：{STATUS_LABELS[ticket['status']]}。"
            '\n工单已保存在系统中，需管理员查看和处理；当前没有实时人工接待或自动通知。')


def admin_list(status='pending', limit=20):
    if status not in STATUS_LABELS or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid list options')
    ensure_handoff_schema()
    with connect_database() as conn:
        return conn.execute('SELECT ticket_id,thread_id,reason,status,assigned_to,version,created_at '
                            'FROM customer_service.human_handoffs WHERE status=%s ORDER BY created_at LIMIT %s',
                            (status, limit)).fetchall()


def admin_show(ticket_id):
    ensure_handoff_schema()
    with connect_database() as conn:
        ticket = conn.execute('SELECT * FROM customer_service.human_handoffs WHERE ticket_id=%s',
                              (ticket_id,)).fetchone()
        if ticket is None:
            raise ValueError('工单不存在')
        events = conn.execute('SELECT event,actor,note,created_at FROM customer_service.human_handoff_events '
                              'WHERE ticket_id=%s ORDER BY event_id', (ticket_id,)).fetchall()
    return {'ticket': ticket, 'events': events}


def admin_transition(ticket_id, action, operator, note=None):
    if not isinstance(operator, str) or not 1 <= len(operator.strip()) <= 100:
        raise ValueError('请提供1–100字符的处理人标记')
    if action not in {'claim', 'resolve'}:
        raise ValueError('invalid action')
    if action == 'resolve' and (not isinstance(note, str) or not 1 <= len(note.strip()) <= 2000):
        raise ValueError('处理完成时请提供1–2000字符的处理说明')
    operator = operator.strip()
    ensure_handoff_schema()
    with connect_database() as conn:
        row = conn.execute('SELECT * FROM customer_service.human_handoffs WHERE ticket_id=%s FOR UPDATE',
                           (ticket_id,)).fetchone()
        if row is None:
            raise ValueError('工单不存在')
        if action == 'claim':
            if row['status'] == 'in_progress' and row['assigned_to'] == operator:
                return _public(row)
            if row['status'] != 'pending':
                raise ValueError('该工单已被领取或完成，不能重复领取')
            status = 'in_progress'
        else:
            if row['status'] != 'in_progress' or row['assigned_to'] != operator:
                raise ValueError('需由领取该工单的处理人完成')
            status = 'resolved'
        row = conn.execute("""UPDATE customer_service.human_handoffs SET status=%s,assigned_to=%s,
            resolution_note=%s,version=version+1,updated_at=now() WHERE ticket_id=%s RETURNING *""",
            (status, operator, note, ticket_id)).fetchone()
        conn.execute('INSERT INTO customer_service.human_handoff_events(ticket_id,event,actor,note) '
                     'VALUES (%s,%s,%s,%s)', (ticket_id, action, operator, note))
    return _public(row)
