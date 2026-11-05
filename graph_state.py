"""Persisted graph-state contract, unchanged by composition refactoring."""
from typing import Annotated,TypedDict,Sequence
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

class SupervisorState(TypedDict):
    """监督者状态"""
    messages: Annotated[Sequence[BaseMessage], add_messages]
    next: str  # 下一个要执行的智能体
    identity_version: int
    identity_customer_id: int | None
    task: dict
    task_result: dict
    pending_messages: Sequence[BaseMessage]
    execution_elapsed_ms: float
    tasks: list[dict]
    turn_task_ids: list[str]
    turn_outputs: list[dict]
    turn_budget: dict
    scheduler_steps: int
    plan_message: str | None
    execution_error: str | None
    execution_catalog_scope: dict | None
    inflight_active: bool
    inflight_replan: bool
    inflight_rounds: int
    inflight_user_ids: list[str]
