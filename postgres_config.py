from pathlib import Path
import os
import math
from request_deadline import remaining_seconds, check_deadline

PROJECT_DIR = Path(__file__).resolve().parent


def load_project_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_DIR / ".env")
    load_dotenv(PROJECT_DIR.parent / "langgraph_example" / ".env")


def connect_database(*, importing: bool = False):
    """连接专用客服 schema；连接串只从环境读取，不在日志中输出。"""
    load_project_env()
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("请在项目 .env 中配置 DATABASE_URL")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError('请先安装 PostgreSQL 驱动：pip install "psycopg[binary]>=3.2,<4"') from exc
    check_deadline()
    timeout = 0 if importing else int(max(1, remaining_seconds(15) * 1000))

    class DeadlineConnection(psycopg.Connection):
        def execute(self, query, params=None, **kwargs):
            check_deadline()
            remaining = remaining_seconds()
            if remaining is not None and not importing:
                milliseconds = max(1, min(15000, int(remaining * 1000)))
                # SET does not take a data snapshot, so a following SET TRANSACTION is still legal.
                super().execute(f'SET LOCAL statement_timeout={milliseconds}')
                super().execute(f'SET LOCAL lock_timeout={milliseconds}')
            result = super().execute(query, params, **kwargs)
            check_deadline()
            return result

        def commit(self):
            if remaining_seconds() == 0:
                self.rollback()
                check_deadline()
            return super().commit()

    return DeadlineConnection.connect(
        dsn,
        connect_timeout=max(2, math.ceil(remaining_seconds(5))),
        row_factory=dict_row,
        options=f"-c search_path=customer_service,public -c statement_timeout={timeout} -c lock_timeout={timeout}",
    )
