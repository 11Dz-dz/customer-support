"""服务器管理的 thread_id -> customer_id 映射；不从聊天或模型参数识别身份。"""
from contextlib import contextmanager
from contextvars import ContextVar
import threading

from conversation_persistence import get_checkpointer, session_lock, validate_thread_id
from postgres_config import connect_database


_schema_lock = threading.Lock()
_schema_ready = False
_identity = ContextVar("customer_service_identity", default=None)
IDENTITY_VERSION = 1


class SessionIdentityError(RuntimeError):
    code = "SESSION_IDENTITY_ERROR"

    def result(self):
        return {"success": False, "error_code": self.code, "error": str(self)}


class SessionUnboundError(SessionIdentityError):
    code = "SESSION_UNBOUND"

    def __init__(self):
        super().__init__("此会话尚未绑定客户，暂不能访问订单。请由服务器管理员配置会话用户映射。")


class IdentityConflictError(SessionIdentityError):
    code = "SESSION_IDENTITY_CONFLICT"


def ensure_identity_schema():
    global _schema_ready
    with _schema_lock:
        if _schema_ready:
            return
        with connect_database() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(48151335)")
            conn.execute("""CREATE TABLE IF NOT EXISTS customer_service.session_customers (
                thread_id text PRIMARY KEY CHECK (length(thread_id) BETWEEN 1 AND 128),
                customer_id integer NOT NULL CHECK (customer_id > 0),
                created_at timestamptz NOT NULL DEFAULT now()
            )""")
        _schema_ready = True


def get_session_customer(thread_id):
    validate_thread_id(thread_id)
    ensure_identity_schema()
    with connect_database() as conn:
        row = conn.execute("SELECT customer_id FROM customer_service.session_customers WHERE thread_id=%s",
                           (thread_id,)).fetchone()
    return row["customer_id"] if row else None


def bind_session_customer(thread_id, customer_id):
    """仅供可信服务器管理入口使用；绑定不可变，不能接成模型工具或公开的免登录接口。"""
    validate_thread_id(thread_id)
    if isinstance(customer_id, bool) or not isinstance(customer_id, int) or not 0 < customer_id <= 2147483647:
        raise ValueError("customer_id 必须是有效的正整数")
    ensure_identity_schema()
    # 与 CLI 执行入口共用会话锁，避免处理消息时绑定身份。
    with session_lock(thread_id):
        with connect_database() as conn:
            existing = conn.execute("SELECT customer_id FROM customer_service.session_customers WHERE thread_id=%s",
                                    (thread_id,)).fetchone()
            if existing:
                if existing["customer_id"] != customer_id:
                    raise IdentityConflictError("此会话已绑定其他客户；请创建新的 thread_id，不能换绑已有会话。")
                return customer_id
            # 数据集没有独立客户表，以订单里已存在的客户 ID 校验演示身份。
            if not conn.execute("SELECT 1 FROM orders WHERE customer_id=%s LIMIT 1", (customer_id,)).fetchone():
                raise ValueError("customer_id 不在当前订单数据中")
            if get_checkpointer().get_tuple({"configurable": {"thread_id": thread_id}}) is not None:
                raise IdentityConflictError("此会话已有历史消息，不能补绑用户；请新建 thread_id 后再绑定。旧历史保留。")
            conn.execute("INSERT INTO customer_service.session_customers(thread_id,customer_id) VALUES (%s,%s)",
                         (thread_id, customer_id))
    return customer_id


def validate_session_state(thread_id, state, *, new_request=False):
    """发送历史给模型或显示历史前，确认检查点身份与当前映射一致。"""
    customer_id = get_session_customer(thread_id)
    version = state.get("identity_version")
    if version is None:
        # 图入口已经追加了本轮 HumanMessage，新建会话此时恰好只有一条消息。
        allowed_messages = 1 if new_request else 0
        if len(state.get("messages", [])) > allowed_messages:
            raise IdentityConflictError("此会话含未标记用户归属的旧版历史。请新建会话后绑定客户；旧历史未删除。")
    elif version != IDENTITY_VERSION or state.get("identity_customer_id") != customer_id:
        raise IdentityConflictError("会话历史身份与当前映射不一致，已停止加载；请使用新会话。")
    return customer_id


def identity_guard_node(state, config):
    thread_id = config.get("configurable", {}).get("thread_id")
    customer_id = validate_session_state(thread_id, state, new_request=True)
    return {"identity_version": IDENTITY_VERSION, "identity_customer_id": customer_id}


@contextmanager
def customer_identity_scope(config, state):
    thread_id = config.get("configurable", {}).get("thread_id")
    customer_id = validate_session_state(thread_id, state)
    token = _identity.set((thread_id, customer_id))
    try:
        yield customer_id
    finally:
        _identity.reset(token)


def current_customer_id(config):
    """工具从可信上下文读取身份，忽略 metadata/customer_id 等外部传值。"""
    thread_id = config.get("configurable", {}).get("thread_id")
    identity = _identity.get()
    if identity is None or identity[0] != thread_id or identity[1] is None:
        raise SessionUnboundError()
    if get_session_customer(thread_id) != identity[1]:
        raise IdentityConflictError("会话用户映射发生变化，已停止访问订单。")
    return identity[1]
