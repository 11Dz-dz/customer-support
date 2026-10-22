"""结构化事件；不记录提示词、工具参数/结果正文、密钥或 reasoning_content。"""
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
import time

from langchain_core.callbacks import BaseCallbackHandler
from tool_result import normalize_result


_logger = logging.getLogger("customer_service.events")
_logger.setLevel(logging.INFO)
_logger.propagate = False


def configure_event_log(path: Path):
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    for handler in list(_logger.handlers):
        _logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)


def emit_event(event_type: str, config=None, **fields):
    metadata = (config or {}).get("metadata", {})
    event = {
        "event": event_type,
        "time": datetime.now(timezone.utc).isoformat(),
        "request_id": metadata.get("request_id"),
        "thread_id": metadata.get("thread_id"),
        **fields,
    }
    _logger.info(json.dumps(event, ensure_ascii=False))
    try:
        from langgraph.config import get_stream_writer
        get_stream_writer()(event)
    except (RuntimeError, KeyError):
        pass  # 普通 invoke 或独立工具测试没有自定义流消费者。
    return event


class ToolEventHandler(BaseCallbackHandler):
    run_inline = True

    def __init__(self):
        self._runs = {}
        self._lock = threading.Lock()

    def on_tool_start(self, serialized, input_str, *, run_id, metadata=None, **kwargs):
        metadata = metadata or {}
        config = {"metadata": metadata}
        name = (serialized or {}).get("name", "unknown_tool")
        with self._lock:
            self._runs[run_id] = (time.perf_counter(), name, config)
        emit_event("tool_start", config, tool=name, agent=metadata.get("agent"), run_id=str(run_id))

    def _finish(self, run_id, success, error_type=None, result=None):
        with self._lock:
            item = self._runs.pop(run_id, None)
        if item is None:
            return
        start, name, config = item
        emit_event("tool_end", config, tool=name, agent=config["metadata"].get("agent"), run_id=str(run_id),
                   success=success, elapsed_ms=round((time.perf_counter() - start) * 1000, 2),
                   error_type=error_type,
                   **({'outcome':result.status,'error_code':result.error_code,'retryable':result.retryable} if result else {}))

    def on_tool_end(self, output, *, run_id, **kwargs):
        with self._lock:entry=self._runs.get(run_id)
        name=entry[1] if entry else ''
        result=normalize_result(getattr(output,'content',output),name,transport_error=getattr(output,'status',None)=='error')
        self._finish(run_id,result.ok,result=result)

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, False, type(error).__name__)
