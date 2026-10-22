"""真实 PostgreSQL 的会话映射和越权回归；假路由/假专业模型，不调用 DeepSeek。"""
import json
from tool_result import business_payload
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage

from adventureworks_service import AdventureWorksService
from chat_runtime import invoke_turn
from conversation_persistence import get_checkpointer
from postgres_config import connect_database
from session_identity import (IDENTITY_VERSION, IdentityConflictError, bind_session_customer,
                              current_customer_id, customer_identity_scope, get_session_customer,
                              validate_session_state)


class RouterStub:
    def __init__(self):
        self.calls = 0

    def invoke(self, messages, config):
        self.calls += 1
        return SimpleNamespace(next="billing")


class BillingStub:
    def invoke(self, state, config):
        from langgraph_supervisor_1 import query_order
        order_id = state["messages"][-1].content
        call = {"name": "query_order", "args": {"order_id": order_id}, "id": "call-" + uuid4().hex}
        result = query_order.invoke({**call, "type": "tool_call"}, config)
        return {"messages": [*state["messages"], AIMessage(content="", tool_calls=[call]), result,
                             AIMessage(content=result.content)]}


class SessionIdentityTest(unittest.TestCase):
    def setUp(self):
        self.thread_ids = []
        self.service = AdventureWorksService()
        with connect_database() as conn:
            self.owner_a = conn.execute("SELECT customer_id FROM orders WHERE order_id=43659").fetchone()["customer_id"]
            self.owner_b = conn.execute("SELECT customer_id FROM orders WHERE order_id=43660").fetchone()["customer_id"]
        self.assertNotEqual(self.owner_a, self.owner_b)
        self.thread_a, self.thread_b, self.guest = (self.thread() for _ in range(3))
        bind_session_customer(self.thread_a, self.owner_a)
        bind_session_customer(self.thread_b, self.owner_b)

    def thread(self):
        tid = "test-identity-" + uuid4().hex
        self.thread_ids.append(tid)
        return tid

    def tearDown(self):
        for tid in self.thread_ids:
            get_checkpointer().delete_thread(tid)
            with connect_database() as conn:
                conn.execute("DELETE FROM customer_service.session_customers WHERE thread_id=%s", (tid,))

    def config(self, tid):
        # 故意伪造可配置字段和 metadata；工具必须忽略它们，使用数据库中的映射。
        return {"configurable": {"thread_id": tid, "customer_id": self.owner_b},
                "metadata": {"customer_id": self.owner_b, "request_id": "test-identity"}}

    def identity_state(self, owner):
        return {"messages": [], "identity_version": IDENTITY_VERSION, "identity_customer_id": owner}

    def test_mapping_survives_new_process_and_binding_is_immutable(self):
        script = "from session_identity import get_session_customer; import sys; print(get_session_customer(sys.argv[1]))"
        result = subprocess.run([sys.executable, "-c", script, self.thread_a], cwd=Path(__file__).resolve().parent.parent,
                                capture_output=True, text=True, check=True, timeout=35)
        self.assertEqual(int(result.stdout.strip()), self.owner_a)
        self.assertEqual(bind_session_customer(self.thread_a, self.owner_a), self.owner_a)
        with self.assertRaises(IdentityConflictError):
            bind_session_customer(self.thread_a, self.owner_b)
        self.assertEqual(get_session_customer(self.thread_a), self.owner_a)

    def test_all_read_services_filter_both_order_and_customer(self):
        for method in (self.service.get_order, self.service.get_payment, self.service.get_invoice):
            self.assertTrue(method("43659", customer_id=self.owner_a)["success"])
            self.assertTrue(method("43660", customer_id=self.owner_b)["success"])
            denied = method("43660", customer_id=self.owner_a)
            missing = method("2147483646", customer_id=self.owner_a)
            self.assertEqual(denied, missing)
            self.assertEqual(denied["error_code"], "ORDER_NOT_ACCESSIBLE")
            self.assertNotIn("customer_id", denied)
            self.assertFalse(method("43659", customer_id=self.owner_b)["success"])

    def test_services_fail_closed_without_identity(self):
        methods = [lambda: self.service.get_order("43659"), lambda: self.service.get_payment("43659"),
                   lambda: self.service.get_invoice("43659"),
                   lambda: self.service.request_refund("43659", "测试")]
        for method in methods:
            self.assertEqual(method()["error_code"], "SESSION_UNBOUND")

    def test_tool_schemas_hide_identity_and_forged_arguments_cannot_change_user(self):
        from langgraph_supervisor_1 import billing_tools, query_order
        for tool in billing_tools:
            fields = tool.tool_call_schema.model_json_schema()["properties"]
            self.assertNotIn("customer_id", fields)
            self.assertNotIn("config", fields)
        config = self.config(self.thread_a)
        with customer_identity_scope(config, self.identity_state(self.owner_a)):
            self.assertEqual(current_customer_id(config), self.owner_a)
            own = query_order.invoke({"order_id": "43659", "customer_id": self.owner_b}, config)
            denied = query_order.invoke({"order_id": "43660", "customer_id": self.owner_b}, config)
        self.assertEqual(business_payload(own,'query_order')["customer_id"], self.owner_a)
        self.assertEqual(denied["error_code"], "ORDER_NOT_ACCESSIBLE")

    def test_all_billing_tools_deny_guest_and_direct_calls_without_scope(self):
        from langgraph_supervisor_1 import billing_tools
        for tool in billing_tools:
            arguments = {"order_id": "43659"}
            if tool.name == "request_refund":
                arguments["reason"] = "权限回归测试"
            config = self.config(self.guest)
            with customer_identity_scope(config, self.identity_state(None)):
                self.assertEqual(tool.invoke(arguments, config)["error_code"], "SESSION_UNBOUND")
            self.assertEqual(tool.invoke(arguments, self.config(self.thread_a))["error_code"], "SESSION_UNBOUND")

    def test_unauthorized_refund_has_no_side_effect_and_owner_refund_is_idempotent(self):
        test_id = 2147483591
        with connect_database() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM orders WHERE order_id=%s", (test_id,)).fetchone())
            conn.execute("""INSERT INTO orders(order_id,order_number,customer_id,status_code,purchase_at,due_at,
                            subtotal,tax_amount,freight,total_due)
                            VALUES (%s,'TEST-IDENTITY-REFUND',%s,5,now(),now(),0,0,0,0)""", (test_id, self.owner_a))
        try:
            result = self.service.request_refund(str(test_id), "权限测试", customer_id=self.owner_b)
            self.assertEqual(result["error_code"], "ORDER_NOT_ACCESSIBLE")
            with connect_database() as conn:
                self.assertEqual(conn.execute("SELECT count(*) AS n FROM refund_requests WHERE order_id=%s", (test_id,)).fetchone()["n"], 0)
            first = self.service.request_refund(str(test_id), "权限测试", customer_id=self.owner_a)
            second = self.service.request_refund(str(test_id), "权限测试", customer_id=self.owner_a)
            self.assertEqual(first["status"], "pending")
            self.assertEqual(first["refund_id"], second["refund_id"])
        finally:
            with connect_database() as conn:
                conn.execute("DELETE FROM refund_requests WHERE order_id=%s", (test_id,))
                conn.execute("DELETE FROM orders WHERE order_id=%s AND order_number='TEST-IDENTITY-REFUND'", (test_id,))

    def test_real_graph_persists_identity_and_isolates_multiple_turns(self):
        import langgraph_supervisor_1 as module
        router = RouterStub()
        with patch.object(module, "supervisor_router", router), patch.object(module, "billing_agent", BillingStub()):
            first = invoke_turn(module.app, "43659", self.thread_a)
            second = invoke_turn(module.app, "43660", self.thread_a)
            other = invoke_turn(module.app, "43660", self.thread_b)
        self.assertEqual(first["identity_customer_id"], self.owner_a)
        self.assertEqual(second["identity_customer_id"], self.owner_a)
        self.assertIn("无权", second["messages"][-1].content)
        denied_result = business_payload(next(m.content for m in reversed(second["messages"]) if isinstance(m, ToolMessage)))
        self.assertEqual(denied_result["error_code"], "ORDER_NOT_ACCESSIBLE")
        own_result = business_payload(next(m.content for m in reversed(other["messages"]) if isinstance(m, ToolMessage)))
        self.assertEqual(own_result["customer_id"], self.owner_b)
        self.assertEqual([m.content for m in other["messages"] if m.type == "human"], ["43660"])
        saved = get_checkpointer().get_tuple({"configurable": {"thread_id": self.thread_a}})
        self.assertEqual(saved.checkpoint["channel_values"]["identity_customer_id"], self.owner_a)

    def test_parallel_toolnode_threads_receive_the_same_verified_identity(self):
        import langgraph_supervisor_1 as module
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langgraph.prebuilt import ToolNode, create_react_agent
        from tests.test_tool_execution import LoopModel
        from tool_execution import guarded_model, guarded_prompt, guarded_tool_call

        class ParallelOrderModel(LoopModel):
            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                if messages[-1].type == "tool":
                    answer = AIMessage(content="已完成归属检查。")
                else:
                    answer = AIMessage(content="", tool_calls=[
                        {"name": "query_order", "args": {"order_id": oid}, "id": "call-" + oid}
                        for oid in ("43659", "43660")])
                return ChatResult(generations=[ChatGeneration(message=answer)])

        tools = [module.query_order]
        agent = create_react_agent(guarded_model(ParallelOrderModel(), tools),
                                  ToolNode(tools, wrap_tool_call=guarded_tool_call),
                                  prompt=guarded_prompt("测试订单归属"), checkpointer=False)
        with patch.object(module, "supervisor_router", RouterStub()), patch.object(module, "billing_agent", agent):
            result = invoke_turn(module.app, "同时检查两个订单", self.thread_a)
        rows = [business_payload(m.content) for m in result["messages"] if isinstance(m, ToolMessage)]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(r.get("success") is True for r in rows), 1)
        self.assertEqual(next(r for r in rows if r.get("success"))["customer_id"], self.owner_a)
        self.assertEqual(next(r for r in rows if not r.get("success"))["error_code"], "ORDER_NOT_ACCESSIBLE")

    def test_legacy_history_is_preserved_but_not_loaded_or_rebound(self):
        from tests.test_chat_runtime import echo_graph
        from langchain_core.messages import HumanMessage
        import langgraph_supervisor_1 as module
        from run_multiturn import display_history
        tid = self.thread()
        echo_graph().invoke({"messages": [HumanMessage(content="legacy-private-marker")]},
                            {"configurable": {"thread_id": tid}})
        router = RouterStub()
        with patch.object(module, "supervisor_router", router):
            with self.assertRaises(IdentityConflictError):
                invoke_turn(module.app, "43659", tid)
        self.assertEqual(router.calls, 0)
        with self.assertRaises(IdentityConflictError):
            display_history(module.app, tid)
        with self.assertRaises(IdentityConflictError):
            bind_session_customer(tid, self.owner_a)
        saved = get_checkpointer().get_tuple({"configurable": {"thread_id": tid}})
        self.assertIn("legacy-private-marker", str(saved.checkpoint["channel_values"]["messages"]))

    def test_guest_conversation_cannot_later_be_bound_to_another_identity(self):
        import langgraph_supervisor_1 as module
        with patch.object(module, "supervisor_router", RouterStub()), patch.object(module, "billing_agent", BillingStub()):
            result = invoke_turn(module.app, "43659", self.guest)
        self.assertIsNone(result["identity_customer_id"])
        self.assertIn("尚未绑定", result["messages"][-1].content)
        unbound_result = business_payload(next(m.content for m in reversed(result["messages"]) if isinstance(m, ToolMessage)))
        self.assertEqual(unbound_result["error_code"], "SESSION_UNBOUND")
        with self.assertRaises(IdentityConflictError):
            bind_session_customer(self.guest, self.owner_a)

    def test_database_mapping_tampering_blocks_history_before_routing(self):
        import langgraph_supervisor_1 as module
        with patch.object(module, "supervisor_router", RouterStub()), patch.object(module, "billing_agent", BillingStub()):
            invoke_turn(module.app, "43659", self.thread_a)
        with connect_database() as conn:
            conn.execute("UPDATE customer_service.session_customers SET customer_id=%s WHERE thread_id=%s",
                         (self.owner_b, self.thread_a))
        router = RouterStub()
        with patch.object(module, "supervisor_router", router):
            with self.assertRaises(IdentityConflictError):
                invoke_turn(module.app, "43660", self.thread_a)
        self.assertEqual(router.calls, 0)
        with self.assertRaises(IdentityConflictError):
            validate_session_state(self.thread_a, module.app.get_state({"configurable": {"thread_id": self.thread_a}}).values)


if __name__ == "__main__":
    unittest.main(verbosity=2)
