"""CLI 共用的流式/一次性执行入口；状态由编译后的图持久化。"""
import time
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage

from backend.observability.chat_events import ToolEventHandler, emit_event
from backend.conversation.conversation_persistence import session_lock, validate_thread_id
from backend.agent.request_deadline import timeout_setting
from backend.observability.langsmith_tracing import trace_turn


def public_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content
                       if isinstance(block, dict) and block.get("type") in {"text", "text_delta"})
    return ""


def final_reply(messages):
    return next((message for message in reversed(messages)
                 if isinstance(message, AIMessage) and not message.tool_calls), None)


def turn_config(thread_id, *, callbacks=None):
    validate_thread_id(thread_id)
    return {
        "configurable": {"thread_id": thread_id},
        "metadata": {"thread_id": thread_id, "request_id": uuid4().hex,
                     "request_deadline_monotonic": time.monotonic() + timeout_setting()},
        "callbacks": [ToolEventHandler(), *(callbacks or [])],
        "recursion_limit": 24,
    }


def stream_turn(app, query: str, thread_id: str, *, callbacks=None):
    from backend.conversation.turn_inbox import current_inbox, TurnStopped, answer_metadata
    inbox = current_inbox()
    config = turn_config(thread_id, callbacks=callbacks)
    if inbox is not None:
        config['recursion_limit'] = 96
    start = time.perf_counter()
    with session_lock(thread_id),trace_turn(config,query,'stream') as tracing:
        emit_event("request_start", config, mode="stream")
        wrote_text = False
        answer = None
        try:
            for part in app.stream(
                {"messages": [HumanMessage(content=query, id=inbox.initial_message_id if inbox else None)],
                 'inflight_active': False, 'inflight_replan': False,
                 'inflight_user_ids': [inbox.initial_message_id] if inbox else []}, config,
                stream_mode=["messages", "custom", "updates"], subgraphs=True, version="v2",
            ):
                if part["type"] == "messages":
                    message, metadata = part["data"]
                    if "assistant_output" not in metadata.get("tags", []):
                        continue
                    # tool_call_chunks 是工具参数，不作为回答显示；reasoning 字段从不读取。
                    if getattr(message, "tool_call_chunks", None):
                        continue
                    text = public_text(message.content)
                    if text:
                        wrote_text = True
                        yield {"type": "text", "text": text, "message_id": message.id}
                elif part["type"] == "custom":
                    data = part["data"]
                    if data.get("event") == "verified_answer_chunk":
                        wrote_text = True
                        yield {"type": "text", "text": data["text"], "message_id": data.get("message_id")}
                    else:
                        yield {"type": "event", "data": data}
                elif part["type"] == "updates" and not part.get("ns"):
                    for update in part["data"].values():
                        if isinstance(update, dict) and "messages" in update:
                            candidate = final_reply(update["messages"])
                            if candidate:
                                answer = candidate
            # 某些兼容服务没有 token 流时，只输出一次最终正文，避免重复打印。
            if not wrote_text and answer is not None:
                yield {"type": "text", "text": public_text(answer.content), "message_id": answer.id}
            tracing.finish(public_text(answer.content) if answer is not None else '')
            event = emit_event("request_end", config, success=True,
                               elapsed_ms=round((time.perf_counter() - start) * 1000, 2))
            yield {"type": "done", "answer": answer, "event": event}
        except TurnStopped:
            text = '已停止后续处理。此前已完成的查询和操作保留，不会因停止而撤销，也不会自动重放。'
            saved = app.get_state(config).values
            ids = set(saved.get('turn_task_ids', []))
            tasks = [{**t, 'status': 'cancelled'} if t['task_id'] in ids and
                     t.get('status') in {'ready', 'awaiting_dependency'} else t for t in saved.get('tasks', [])]
            answer = AIMessage(content=text, id=uuid4().hex,additional_kwargs=answer_metadata())
            app.update_state(config, {'messages': [answer], 'tasks': tasks, 'pending_messages': [],
                                     'inflight_active': False}, as_node='answer')
            yield {'type': 'text', 'text': text, 'message_id': answer.id}
            emit_event('request_end', config, success=False, error_type='TurnStopped',
                       elapsed_ms=round((time.perf_counter()-start)*1000, 2))
            tracing.finish(text)
            raise
        except BaseException as exc:
            emit_event("request_end", config, success=False, error_type=type(exc).__name__,
                       elapsed_ms=round((time.perf_counter() - start) * 1000, 2))
            raise


def invoke_turn(app, query: str, thread_id: str, *, callbacks=None):
    config = turn_config(thread_id, callbacks=callbacks)
    start = time.perf_counter()
    with session_lock(thread_id),trace_turn(config,query,'invoke') as tracing:
        emit_event("request_start", config, mode="invoke")
        try:
            result = app.invoke({"messages": [HumanMessage(content=query)],'inflight_active':False,
                                 'inflight_replan':False,'inflight_user_ids':[]}, config)
            answer=final_reply(result.get('messages',[]))
            tracing.finish(public_text(answer.content) if answer is not None else '')
            emit_event("request_end", config, success=True,
                       elapsed_ms=round((time.perf_counter() - start) * 1000, 2))
            return result
        except BaseException as exc:
            emit_event("request_end", config, success=False, error_type=type(exc).__name__,
                       elapsed_ms=round((time.perf_counter() - start) * 1000, 2))
            raise
