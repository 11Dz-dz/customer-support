"""PostgreSQL 检查点与同一会话的跨进程互斥。"""
from contextlib import contextmanager
import atexit
import os
import re
import threading

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import connect
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from postgres_config import load_project_env


_lock = threading.Lock()
_pool = None
_saver = None
_OPTIONS = "-c search_path=agent_checkpoints,public -c statement_timeout=15000"


def database_url():
    load_project_env()
    value = (os.getenv("CHECKPOINT_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    if not value:
        raise RuntimeError("请在项目 .env 中配置 DATABASE_URL")
    return value


def get_checkpointer():
    global _pool, _saver
    with _lock:
        if _saver is not None:
            return _saver
        dsn = database_url()
        # setup 的迁移包含不能放入事务的索引操作，必须使用 autocommit。
        with connect(dsn, autocommit=True, row_factory=dict_row, prepare_threshold=0,
                     connect_timeout=5, options=_OPTIONS) as conn:
            conn.execute("CREATE SCHEMA IF NOT EXISTS agent_checkpoints")
            conn.execute("SELECT pg_advisory_lock(48151333)")
            try:
                PostgresSaver(conn).setup()
            finally:
                conn.execute("SELECT pg_advisory_unlock(48151333)")
        pool = ConnectionPool(
            conninfo=dsn, min_size=1, max_size=5, timeout=8, open=False,
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0,
                    "connect_timeout": 5, "options": _OPTIONS},
        )
        pool.open(wait=True, timeout=8)
        _pool, _saver = pool, PostgresSaver(pool)
        return _saver


def close_persistence():
    global _pool, _saver
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool = _saver = None


def validate_thread_id(thread_id: str):
    if not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9_:.\-]{1,128}", thread_id):
        raise ValueError("thread_id 需为 1–128 个字母、数字、下划线、冒号、点或连字符")
    return thread_id


class SessionBusyError(RuntimeError):
    pass


@contextmanager
def session_lock(thread_id: str):
    """一个会话同一时刻只处理一轮请求，避免两个进程写入互相覆盖。"""
    validate_thread_id(thread_id)
    get_checkpointer()
    with _pool.connection() as conn:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,48151334)) AS acquired", (thread_id,),
        ).fetchone()
        if not row["acquired"]:
            raise SessionBusyError("这个会话正在处理另一条消息，请稍后重试")
        try:
            yield
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,48151334))", (thread_id,))


atexit.register(close_persistence)
