"""显式运行的 DeepSeek 端到端回归；消耗 API Token，只清理本脚本的随机测试会话。"""
import argparse
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage

from chat_events import configure_event_log
from chat_runtime import final_reply, invoke_turn, stream_turn
from conversation_persistence import close_persistence, get_checkpointer
from langgraph_supervisor_1 import app
from postgres_config import connect_database
from session_identity import bind_session_customer
from tool_result import business_payload


def tool_results(messages):
    results = []
    for message in messages:
        if isinstance(message, ToolMessage):
            try:
                results.append((message.name, business_payload(message.content,message.name,
                                transport_error=message.status=='error')))
            except (ValueError, TypeError):
                pass
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["highest", "lowest", "budget", "count", "stats",
                                            "recommendation", "policy", "conversation"])
    args = parser.parse_args()
    configure_event_log(Path(__file__).resolve().parent / "logs" / "events.jsonl")
    threads, reports = [], []
    def new_thread():
        tid = "eval-query-" + uuid4().hex
        threads.append(tid)
        return tid

    with connect_database() as conn:
        truth = conn.execute("SELECT max(list_price) AS hi,min(list_price) AS lo FROM product_catalog WHERE list_price>0").fetchone()
        road_count = conn.execute("SELECT count(*) AS n FROM product_catalog WHERE name ILIKE '%Road-150%' AND list_price>0").fetchone()["n"]
        order_customer_id = conn.execute("SELECT customer_id FROM orders WHERE order_id=43659").fetchone()["customer_id"]

    def run(label, query, check, *, stream=False):
        tid = new_thread()
        if stream:
            parts = list(stream_turn(app, query, tid))
            assert any(p["type"] == "text" for p in parts), "无流式正文"
            assert parts[-1]["type"] == "done"
            messages = app.get_state({"configurable": {"thread_id": tid}}).values["messages"]
        else:
            messages = invoke_turn(app, query, tid)["messages"]
        calls = [c for m in messages if isinstance(m, AIMessage) for c in m.tool_calls]
        assert len(calls) <= 6, "超过工具请求预算"
        assert final_reply(messages), "缺少最终回答"
        try:
            check(calls, tool_results(messages))
        except AssertionError:
            failure = {"case": label, "passed": False, "tool_calls": calls,
                       "answer_excerpt": final_reply(messages).content[:500],
                       "results": [{"tool": name, "success": r.get("success"),
                                    "error": r.get("error"), "returned_count": len(r.get("products", [])),
                                    "price_filters": r.get("price_filters"),
                                    "retrieval_mode": r.get("retrieval_mode")}
                                   for name, r in tool_results(messages)]}
            reports.append(failure)
            print(json.dumps(failure, ensure_ascii=False), flush=True)
            raise
        report = {"case": label, "passed": True, "tool_calls": [c["name"] for c in calls],
                  "answer_excerpt": final_reply(messages).content[:280]}
        reports.append(report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return tid, messages

    def query_result(results):
        return next(r for name, r in results if name == "query_products" and r.get("success"))

    def highest(calls, results):
        r = query_result(results)
        assert not any(c["name"] in {"search_products", "calculate_price"} for c in calls)
        assert Decimal(r["products"][0]["list_price"]) == truth["hi"]

    def lowest(calls, results):
        r = query_result(results)
        assert len(r["products"]) == 5
        assert Decimal(r["products"][0]["list_price"]) == truth["lo"]
        prices = [Decimal(p["list_price"]) for p in r["products"]]
        assert prices == sorted(prices) and min(prices) > 0

    def budget(calls, results):
        r = query_result(results)
        assert len(r["products"]) == 3
        prices = [Decimal(p["list_price"]) for p in r["products"]]
        assert prices == sorted(prices, reverse=True) and max(prices) <= 1000

    def count(calls, results):
        r = query_result(results)
        assert r["operation"] == "count" and r["matched_count"] == road_count

    def stats(calls, results):
        r = query_result(results)
        assert r["operation"] == "stats" and Decimal(r["statistics"]["max_price"]) == truth["hi"]

    def recommendation(calls, results):
        search_calls = [c for c in calls if c["name"] == "search_products"]
        assert search_calls and all(c["args"].get("max_price") == 1000 for c in search_calls)
        products = [p for name, r in results if name == "search_products" for p in r.get("products", [])]
        assert products and all(0 < Decimal(p["list_price"]) <= 1000 for p in products)

    def no_refund(calls, results):
        assert not any(c["name"] == "request_refund" for c in calls)

    try:
        cases = [
            ("highest", "最高价＋流式", "你们这里最贵的商品是什么？", highest),
            ("lowest", "最低价前五名", "列出整个目录中有有效标价的最便宜的五件商品。", lowest),
            ("budget", "预算内排序", "预算1000以内，按标价从高到低列出前三件商品。", budget),
            ("count", "型号计数", "名称包含 Road-150 且有有效标价的商品一共有多少个？", count),
            ("stats", "价格统计", "统计整个目录中有效标价商品的最低价、最高价和平均价格。", stats),
            ("recommendation", "语义推荐＋预算", "预算1000以内，推荐适合长途骑行的商品并解释用途。", recommendation),
            ("policy", "咨询不触发退款", "查询退款政策，不要申请退款。", no_refund),
        ]
        for key, label, query, check in cases:
            if args.case is None or args.case == key:
                run(label, query, check, stream=key == "highest")
        if args.case is None or args.case == "conversation":
            tid = new_thread()
            bind_session_customer(tid, order_customer_id)
            first = invoke_turn(app, "查询订单43659，告诉我第一个商品的编号。", tid)
            order = next(r for name, r in tool_results(first["messages"]) if name == "query_order")
            product_id = order["items"][0]["product_id"]
            second = invoke_turn(app, "刚才第一个商品有什么特点？", tid)
            delta = second["messages"][len(first["messages"]):]
            products = [r["product"]["product_id"] for name, r in tool_results(delta)
                        if name == "get_product_info" and "product" in r]
            # 订单结果已有描述时允许直接复用，不能为了测试强制多查一次。
            if products:
                assert product_id in products, "多轮追问查询了错误商品"
            answer = final_reply(delta)
            expected_name = order["items"][0]["product_name"].split()[0]
            assert answer and (str(product_id) in answer.content or expected_name in answer.content), "追问未对应原商品"
            reports.append({"case": "订单到商品的多轮追问", "passed": True, "product_id": product_id,
                            "reused_order_context": not products})
            print(json.dumps(reports[-1], ensure_ascii=False), flush=True)
    finally:
        for tid in threads:
            get_checkpointer().delete_thread(tid)
            with connect_database() as conn:
                conn.execute("DELETE FROM customer_service.session_customers WHERE thread_id=%s", (tid,))
        close_persistence()
        output = Path(__file__).resolve().parent / "logs" / "query_evaluation_latest.json"
        output.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"通过 {len(reports)} 个端到端场景", flush=True)


if __name__ == "__main__":
    main()
