"""仅评测进程启用的轨迹观察器与原始订单写入阻断。"""
import copy
import json
import threading
import time

from langchain_core.callbacks import BaseCallbackHandler
from tool_execution import READ_ONLY_TOOLS
from tool_result import business_payload


class EvaluationSafetyError(RuntimeError):
    pass


class EvaluationProviderUnavailable(RuntimeError):
    def __init__(self, status_code):
        super().__init__("模型服务身份、权限或余额不可用，评测停止")
        self.status_code = status_code


def parse_output(output):
    content = getattr(output, "content", output)
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return content
    if isinstance(content,dict) and 'schema_version' in content:
        return business_payload(content, getattr(output,'name','') or '',transport_error=getattr(output,'status',None)=='error')
    return content


class EvaluationObserver(BaseCallbackHandler):
    run_inline = True
    raise_error = True  # 写入拦截必须传播异常，不能只记录回调错误后继续执行工具。

    def __init__(self, writable_order_ids):
        self.writable_order_ids = set(writable_order_ids)
        self.tools, self.models, self.violations = [], [], []
        self._tool_runs, self._model_runs = {}, {}
        self._lock = threading.Lock()
        self.terminal_status = None

    def on_tool_start(self, serialized, input_str, *, run_id, inputs=None, **kwargs):
        name = (serialized or {}).get("name", "unknown")
        arguments = inputs if isinstance(inputs, dict) else {}
        if name not in READ_ONLY_TOOLS:
            text = str(arguments.get("order_id", "")).upper()
            text = text[2:] if text.startswith("SO") else text
            allowed = name == "request_refund" and text.isdecimal() and int(text) in self.writable_order_ids
            if not allowed:
                with self._lock:
                    self.violations.append({"type": "write_outside_fixture", "tool": name})
                raise EvaluationSafetyError("评测禁止对临时订单之外的数据执行写入")
        item = {"run_id": str(run_id), "name": name, "args": copy.deepcopy(arguments),
                "success": None, "result": None, "elapsed_ms": None}
        with self._lock:
            self.tools.append(item)
            self._tool_runs[run_id] = (time.perf_counter(), item)

    def on_tool_end(self, output, *, run_id, **kwargs):
        result = parse_output(output)
        with self._lock:
            start, item = self._tool_runs.pop(run_id)
            item.update(result=result, success=getattr(output, "status", None) != "error"
                        and not (isinstance(result, dict) and result.get("success") is False),
                        elapsed_ms=round((time.perf_counter() - start) * 1000, 2))

    def on_tool_error(self, error, *, run_id, **kwargs):
        with self._lock:
            entry = self._tool_runs.pop(run_id, None)
            if entry:
                start, item = entry
                item.update(success=False, error_type=type(error).__name__,
                            elapsed_ms=round((time.perf_counter() - start) * 1000, 2))

    def on_chat_model_start(self, serialized, messages, *, run_id, tags=None, metadata=None, **kwargs):
        if self.terminal_status is not None:
            # 路由器故障可能触发应用兜底；阻止兜底再次发送已知不可用的模型请求。
            raise EvaluationProviderUnavailable(self.terminal_status)
        metadata = metadata or {}
        item = {"role": "router" if "router" in (tags or []) else metadata.get("agent", "specialist"),
                "input_tokens": None, "output_tokens": None, "total_tokens": None,
                "success": None, "elapsed_ms": None}
        with self._lock:
            self.models.append(item)
            self._model_runs[run_id] = (time.perf_counter(), item)

    def on_llm_end(self, response, *, run_id, **kwargs):
        usage = None
        generations = getattr(response, "generations", [])
        if generations and generations[0]:
            usage = getattr(getattr(generations[0][0], "message", None), "usage_metadata", None)
        if not usage:
            raw = (getattr(response, "llm_output", None) or {}).get("token_usage", {})
            if raw:
                usage = {"input_tokens": raw.get("prompt_tokens"), "output_tokens": raw.get("completion_tokens"),
                         "total_tokens": raw.get("total_tokens")}
        with self._lock:
            entry = self._model_runs.pop(run_id, None)
            if entry:
                start, item = entry
                item.update(success=True, elapsed_ms=round((time.perf_counter() - start) * 1000, 2))
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    if usage and isinstance(usage.get(key), int):
                        item[key] = usage[key]

    def on_llm_error(self, error, *, run_id, **kwargs):
        with self._lock:
            entry = self._model_runs.pop(run_id, None)
            if entry:
                start, item = entry
                item.update(success=False, error_type=type(error).__name__,
                            provider_status_code=getattr(error, "status_code", None),
                            elapsed_ms=round((time.perf_counter() - start) * 1000, 2))
            status = getattr(error, "status_code", None)
            if status in {401, 402, 403}:
                self.terminal_status = status
