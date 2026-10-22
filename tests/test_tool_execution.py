"""真实 ToolNode 并发执行测试；假工具、假模型，不调用 DeepSeek。"""
import json
import threading
import time
import unittest
from typing import TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode, create_react_agent
from langgraph.graph import StateGraph, START, END

from tool_execution import TurnPolicy, guarded_model, guarded_prompt, guarded_tool_call, tool_execution_scope
from tool_result import business_payload


class ToolState(TypedDict):
    messages: list


class LoopModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self):
        return "test-loop-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if "已进入最终回答阶段" in messages[0].content:
            answer = AIMessage(content="已有信息不足，请补充条件。")
        else:
            answer = AIMessage(content="", tool_calls=[
                {"name": "lookup", "args": {"value": self.calls}, "id": f"loop-{self.calls}"},
            ])
        return ChatResult(generations=[ChatGeneration(message=answer)])


class ToolExecutionTest(unittest.TestCase):
    @staticmethod
    def run_calls(tools, calls):
        message = AIMessage(content="", tool_calls=[
            {"name": name, "args": args, "id": f"call-{i}"} for i, (name, args) in enumerate(calls)
        ])
        graph = StateGraph(ToolState)
        graph.add_node("tools", ToolNode(tools, wrap_tool_call=guarded_tool_call))
        graph.add_edge(START, "tools")
        graph.add_edge("tools", END)
        return graph.compile().invoke({"messages": [message]})["messages"]

    def test_parallel_duplicates_execute_once_and_keep_distinct_call_ids(self):
        executions = []
        lock = threading.Lock()
        @tool("get_product_info")
        def lookup(product_name: str, detail: bool = True) -> dict:
            """模拟查询。"""
            with lock:
                executions.append(product_name)
            time.sleep(0.05)  # 制造同时在途的调用，验证 single-flight 而非仅完成后缓存。
            return {"success": True, "product_id": product_name}
        with tool_execution_scope():
            messages = self.run_calls([lookup], [
                ("get_product_info", {"product_name": "776"}),
                ("get_product_info", {"product_name": "776", "detail": True}),
                ("get_product_info", {"product_name": "776"}),
            ])
        self.assertEqual(executions, ["776"])
        self.assertEqual({m.tool_call_id for m in messages}, {"call-0", "call-1", "call-2"})
        self.assertTrue(all(business_payload(m.content,m.name)["success"] for m in messages))

    def test_concurrent_batch_cannot_exceed_total_or_search_budget(self):
        executions = []
        @tool("search_products")
        def search(query: str) -> dict:
            """模拟搜索。"""
            executions.append(query)
            return {"success": True}
        with tool_execution_scope(TurnPolicy(max_tool_calls=3, max_search_calls=2)):
            messages = self.run_calls([search], [("search_products", {"query": str(i)}) for i in range(10)])
        self.assertEqual(len(executions), 2)
        self.assertEqual(sum(m.status == "error" for m in messages), 8)

    def test_new_turn_does_not_reuse_stale_result(self):
        executions = []
        @tool("query_order")
        def lookup(order_id: str) -> dict:
            """模拟订单。"""
            executions.append(order_id)
            return {"success": True}
        for _ in range(2):
            with tool_execution_scope():
                self.run_calls([lookup], [("query_order", {"order_id": "43659"})])
        self.assertEqual(len(executions), 2)

    def test_write_tools_and_failed_reads_are_not_cached(self):
        executions = []
        @tool("request_refund")
        def write(order_id: str) -> dict:
            """模拟写入，不操作真实数据库。"""
            executions.append("write")
            return {"success": True}
        @tool("query_order")
        def read(order_id: str) -> dict:
            """模拟失败的查询。"""
            executions.append("failed_read")
            return {"success": False}
        with tool_execution_scope():
            for _ in range(2):
                self.run_calls([write, read], [("request_refund", {"order_id": "1"}),
                                              ("query_order", {"order_id": "1"})])
        self.assertEqual(executions.count("write"), 2)
        self.assertEqual(executions.count("failed_read"), 2)

    def test_expired_turn_blocks_execution(self):
        executions = []
        @tool("query_order")
        def lookup(order_id: str) -> dict:
            """模拟查询。"""
            executions.append(order_id)
            return {"success": True}
        with tool_execution_scope(TurnPolicy(timeout_seconds=0)):
            messages = self.run_calls([lookup], [("query_order", {"order_id": "1"})])
        self.assertEqual(executions, [])
        self.assertEqual(json.loads(messages[0].content)["error_code"], "TURN_DEADLINE")

    def test_loop_is_finalized_with_a_bounded_number_of_model_calls(self):
        @tool("lookup")
        def lookup(value: int) -> dict:
            """模拟需要继续推理的工具。"""
            return {"success": True, "value": value}
        model = LoopModel()
        agent = create_react_agent(guarded_model(model, [lookup]),
                                  ToolNode([lookup], wrap_tool_call=guarded_tool_call),
                                  prompt=guarded_prompt("测试助手"), checkpointer=False)
        with tool_execution_scope(TurnPolicy(max_model_calls=3)):
            result = agent.invoke({"messages": [HumanMessage(content="继续查询")]})
        self.assertEqual(model.calls, 3)
        self.assertFalse(result["messages"][-1].tool_calls)
        self.assertEqual(result["messages"][-1].content, "已有信息不足，请补充条件。")


if __name__ == "__main__":
    unittest.main(verbosity=2)
