"""Retain already completed tool evidence if the following model call times out."""
import json
import threading
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, ToolMessage
from tool_result import normalize_tool_message, exception_result


class TaskEvidenceRecorder(BaseCallbackHandler):
    run_inline = True

    def __init__(self):
        self._lock = threading.Lock()
        self._runs = {}
        self._records = []

    def on_tool_start(self, serialized, input_str, *, run_id, inputs=None, tool_call_id=None, **kwargs):
        try:
            args = inputs if isinstance(inputs, dict) else json.loads(input_str)
        except (ValueError, TypeError):
            args = {}
        with self._lock:
            self._runs[run_id] = ((serialized or {}).get('name', ''), args, tool_call_id)

    def on_tool_end(self, output, *, run_id, **kwargs):
        with self._lock:
            name, args, _ = self._runs.pop(run_id, ('', {}, None))
            if not isinstance(output, ToolMessage):
                return
            name = output.name or name
            call = {'name': name, 'args': args, 'id': output.tool_call_id}
            self._records.extend([AIMessage(content='', tool_calls=[call]),
                                  normalize_tool_message(output, name)])

    def on_tool_error(self, error, *, run_id, **kwargs):
        with self._lock:
            name, args, call_id = self._runs.pop(run_id, ('', {}, None))
            if call_id:
                result = exception_result(error)
                if name == 'request_refund':
                    result = result.model_copy(update={'retryable': False})
                self._records.extend([
                    AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': call_id}]),
                    ToolMessage(content=result.model_dump_json(), name=name, tool_call_id=call_id, status='error')])

    def recovered(self, seed=()):
        with self._lock:
            records = list(seed)
            seen = {m.tool_call_id for m in records if isinstance(m, ToolMessage)}
            for i in range(0, len(self._records), 2):
                pair = self._records[i:i + 2]
                if pair[1].tool_call_id not in seen:
                    records.extend(pair)
                    seen.add(pair[1].tool_call_id)
            return records
