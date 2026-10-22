from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage

from chat_events import configure_event_log
from chat_runtime import final_reply, invoke_turn, public_text, stream_turn
from conversation_persistence import SessionBusyError, close_persistence, validate_thread_id
from session_identity import (SessionIdentityError, get_session_customer, bind_session_customer,
                              validate_session_state)


AGENT_NAMES = {"tech_support": "技术支持", "sales": "销售顾问", "billing": "订单与账单客服"}


def print_trace(event):
    kind = event.get("event")
    if kind == "route":
        print(f"\n🔀 路由到：{AGENT_NAMES.get(event.get('agent'), event.get('agent'))}", flush=True)
    elif kind == "tool_start":
        print(f"\n🔧 调用工具：{event.get('tool')}", flush=True)
    elif kind == "tool_end":
        status = "完成" if event.get("success") else "失败"
        print(f"\n🔧 工具{status}：{event.get('tool')}（{event.get('elapsed_ms')} ms）", flush=True)
    elif kind == "tool_cache_hit":
        print(f"\n♻️ 复用工具结果：{event.get('tool')}", flush=True)
    elif kind == "tool_blocked":
        print(f"\n⛔ 工具调用受限：{event.get('tool')}（{event.get('reason')}）", flush=True)
    elif kind == "access_denied":
        print(f"\n🔒 订单访问受限（{event.get('reason')}）", flush=True)
    elif kind == "request_end":
        print(f"\n⏱ 本轮耗时：{event.get('elapsed_ms')} ms；request_id={event.get('request_id')}", flush=True)
    elif kind in {'handoff_created', 'handoff_reused'}:
        print(f"\n📋 人工工单：{event.get('ticket_id')}（{event.get('status')}）", flush=True)
    elif kind == 'model_timeout':
        print('\n⏳ 模型响应等待已到上限，正在整理已有结果。', flush=True)


def display_history(app, thread_id):
    snapshot = app.get_state({"configurable": {"thread_id": thread_id}})
    validate_session_state(thread_id, snapshot.values)
    visible = [m for m in snapshot.values.get("messages", [])
               if isinstance(m, HumanMessage) or (isinstance(m, AIMessage) and not m.tool_calls)]
    if not visible:
        print("此会话暂无历史消息。")
    for message in visible:
        label = "你" if isinstance(message, HumanMessage) else "小智"
        print(f"\n{label}: {public_text(message.content)}")


def run_multiturn_chat(thread_id: str, *, streaming: bool = True, verbose: bool = False) -> None:
    from langgraph_supervisor_1 import app

    validate_thread_id(thread_id)
    print("=" * 70)
    print("AdventureWorks 多 Agent 客服（PostgreSQL 持久化会话）")
    print("quit / exit / 退出：结束；/history：查看历史；/new：同一用户新建会话")
    print('/human：请求人工处理；/handoffs：查看本会话工单（不依赖模型服务）')
    print(f"thread_id: {thread_id}")
    customer_id = get_session_customer(thread_id)
    print(f"已绑定客户：{customer_id}" if customer_id is not None else "当前为游客会话：可咨询商品，订单功能需要管理员绑定客户。")
    print("重启后使用相同 --thread-id 可继续会话。")
    snapshot = app.get_state({"configurable": {"thread_id": thread_id}})
    try:
        validate_session_state(thread_id, snapshot.values)
        if snapshot.values.get("messages"):
            print(f"已恢复历史状态，共 {len(snapshot.values['messages'])} 条消息（含工具记录）。")
    except SessionIdentityError as exc:
        print(f"🔒 {exc} 输入 /new 可新建会话。")
    print("=" * 70)

    while True:
        try:
            query = input("\n👤 你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n会话已结束。")
            break
        if query.lower() in {"quit", "exit", "退出"}:
            print("会话已结束。")
            break
        if not query:
            print("请输入问题。")
            continue
        if query == "/history":
            try:
                display_history(app, thread_id)
            except SessionIdentityError as exc:
                print(f"🔒 {exc}")
            continue
        if query == "/new":
            customer_id = get_session_customer(thread_id)
            new_thread_id = f"cli-{uuid4().hex[:12]}"
            if customer_id is not None:
                bind_session_customer(new_thread_id, customer_id)
            thread_id = new_thread_id
            print(f"已新建会话，thread_id: {thread_id}；客户：{customer_id if customer_id is not None else '游客'}")
            continue

        try:
            if not streaming:
                result = invoke_turn(app, query, thread_id)
                answer = final_reply(result.get("messages", []))
                print(f"\n🤖 小智: {public_text(answer.content) if answer else '暂时没有生成回复。'}")
                continue
            started = False
            previous_message = None
            for item in stream_turn(app, query, thread_id):
                if item["type"] == "event":
                    if verbose:
                        print_trace(item["data"])
                elif item["type"] == "text":
                    if not started:
                        print("\n🤖 小智: ", end="", flush=True)
                        started = True
                    elif previous_message and item.get("message_id") != previous_message:
                        print("\n", flush=True)
                    print(item["text"], end="", flush=True)
                    previous_message = item.get("message_id")
                elif item["type"] == "done" and verbose:
                    print_trace(item["event"])
            print(flush=True)
        except KeyboardInterrupt:
            print("\n本轮已中断，已写入的检查点会保留；可继续输入或退出。")
        except SessionBusyError as exc:
            print(f"\n❌ {exc}")
        except SessionIdentityError as exc:
            print(f"\n🔒 {exc}")
        except Exception as exc:
            print(f"\n❌ 本轮处理失败（{type(exc).__name__}），请检查事件日志和服务状态。")


def main() -> None:
    parser = argparse.ArgumentParser(description="支持持久化和流式回答的多 Agent 客服")
    parser.add_argument("--thread-id", default=os.getenv("THREAD_ID") or f"cli-{uuid4().hex[:12]}",
                        help="同一 ID 可跨进程恢复历史；不同会话使用不同 ID")
    parser.add_argument("--verbose", action="store_true", help="显示路由、工具调用及耗时")
    parser.add_argument("--no-stream", action="store_true", help="使用一次性回答，仍保留持久化")
    parser.add_argument("--log-file", type=Path,
                        default=Path(__file__).resolve().parent / "logs" / "events.jsonl")
    args = parser.parse_args()
    validate_thread_id(args.thread_id)
    configure_event_log(args.log_file)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    try:
        run_multiturn_chat(args.thread_id, streaming=not args.no_stream, verbose=args.verbose)
    finally:
        close_persistence()


if __name__ == "__main__":
    main()
