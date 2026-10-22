"""持久化/流式/日志测试；使用真实 PostgreSQL 和假模型，不消耗 DeepSeek Token。"""
import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Annotated, TypedDict
import unittest
from uuid import uuid4

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

from chat_events import ToolEventHandler, configure_event_log
from chat_runtime import public_text, stream_turn
from conversation_persistence import get_checkpointer, session_lock


class State(TypedDict):
    messages: Annotated[list, add_messages]


def echo_graph():
    builder = StateGraph(State)
    def echo(state):
        call_id = "call-" + uuid4().hex
        return {"messages": [
            AIMessage(content="", tool_calls=[{"name": "lookup", "args": {}, "id": call_id}]),
            ToolMessage(content="test tool result", tool_call_id=call_id),
            AIMessage(content="echo:" + state["messages"][-1].content),
        ]}
    builder.add_node("echo", echo)
    builder.add_edge(START, "echo")
    builder.add_edge("echo", END)
    return builder.compile(checkpointer=get_checkpointer())


class ChatRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.thread_ids = []

    def thread_id(self):
        value = "test-runtime-" + uuid4().hex
        self.thread_ids.append(value)
        return value

    def tearDown(self):
        saver = get_checkpointer()
        for thread_id in self.thread_ids:
            saver.delete_thread(thread_id)

    def test_messages_and_tool_results_survive_new_process(self):
        thread_id = self.thread_id()
        graph = echo_graph()
        graph.invoke({"messages": [HumanMessage(content="persistent-marker")]},
                     {"configurable": {"thread_id": thread_id}})
        script = """
import json,sys
from conversation_persistence import get_checkpointer
item=get_checkpointer().get_tuple({'configurable':{'thread_id':sys.argv[1]}})
messages=item.checkpoint['channel_values']['messages']
print(json.dumps({'types':[m.type for m in messages], 'contents':[m.content for m in messages]}))
"""
        result = subprocess.run([sys.executable, "-c", script, thread_id],
                                cwd=Path(__file__).resolve().parent.parent, capture_output=True,
                                text=True, timeout=35, check=True)
        saved = json.loads(result.stdout)
        self.assertEqual(saved["types"], ["human", "ai", "tool", "ai"])
        self.assertIn("test tool result", saved["contents"])
        self.assertEqual(saved["contents"][-1], "echo:persistent-marker")

    def test_two_threads_do_not_share_state(self):
        graph = echo_graph()
        threads = [self.thread_id(), self.thread_id()]
        def invoke(pair):
            tid, text = pair
            return graph.invoke({"messages": [HumanMessage(content=text)]},
                                {"configurable": {"thread_id": tid}})
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(invoke, zip(threads, ["session-A", "session-B"])))
        self.assertEqual(results[0]["messages"][-1].content, "echo:session-A")
        self.assertEqual(results[1]["messages"][-1].content, "echo:session-B")
        self.assertNotIn("session-A", str(results[1]["messages"]))

    def test_same_thread_is_locked_between_processes(self):
        tid = self.thread_id()
        script = """
import sys
from conversation_persistence import session_lock,SessionBusyError
try:
 with session_lock(sys.argv[1]): pass
except SessionBusyError:
 print('busy')
else:
 raise AssertionError('concurrent same-thread request was not blocked')
"""
        with session_lock(tid):
            result = subprocess.run([sys.executable, "-c", script, tid], cwd=Path(__file__).resolve().parent.parent,
                                    capture_output=True, text=True, timeout=35, check=True)
        self.assertEqual(result.stdout.strip(), "busy")

    def test_stream_filters_router_and_preserves_final_answer(self):
        router = FakeListChatModel(responses=['{"next":"sales"}'], tags=["router"])
        model = FakeListChatModel(responses=["逐字输出测试"], tags=["assistant_output"])
        builder = StateGraph(State)
        def route(state):
            router.invoke(state["messages"])
            return {}
        builder.add_node("router", route)
        builder.add_node("sales", lambda state: {"messages": [model.invoke(state["messages"])]})
        builder.add_edge(START, "router")
        builder.add_edge("router", "sales")
        builder.add_edge("sales", END)
        graph = builder.compile(checkpointer=get_checkpointer())
        parts = list(stream_turn(graph, "hello", self.thread_id()))
        tokens = [p["text"] for p in parts if p["type"] == "text"]
        self.assertGreater(len(tokens), 1)
        self.assertEqual("".join(tokens), "逐字输出测试")
        self.assertEqual(parts[-1]["type"], "done")
        self.assertEqual(parts[-1]["answer"].content, "逐字输出测试")

    def test_logging_does_not_store_arguments_or_result_text(self):
        @tool
        def sample_tool(secret: str) -> dict:
            """测试日志脱敏。"""
            return {"success": False, "secret": secret}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            configure_event_log(path)
            sample_tool.invoke({"secret": "SHOULD_NOT_APPEAR"}, {
                "callbacks": [ToolEventHandler()],
                "metadata": {"request_id": "request-test", "thread_id": "thread-test"},
            })
            text = path.read_text()
            self.assertNotIn("SHOULD_NOT_APPEAR", text)
            events = [json.loads(line) for line in text.splitlines()]
            self.assertEqual([e["event"] for e in events], ["tool_start", "tool_end"])
            self.assertFalse(events[-1]["success"])
            self.assertIn("elapsed_ms", events[-1])
            from chat_events import _logger
            for handler in list(_logger.handlers):
                handler.close()
                _logger.removeHandler(handler)

    def test_reasoning_blocks_are_not_public_output(self):
        self.assertEqual(public_text([
            {"type": "reasoning", "text": "hidden reasoning"},
            {"type": "text", "text": "回答正文"},
            {"type": "tool_use", "input": {"secret": "value"}},
        ]), "回答正文")


if __name__ == "__main__":
    unittest.main(verbosity=2)
