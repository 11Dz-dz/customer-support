"""SDK integration: scoped skill instructions keep the existing execution guard.

Uses the real LangGraph runtime and a scripted model, not a remote LLM.
"""
import json
import unittest
from unittest.mock import patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from backend.agent.answer_contract import PLAN_INSTRUCTION
from backend.agent.prompt_catalog import read_prompt
from backend.agent.specialist_config import build_specialist, load_specialist_specs
from backend.tasks.task_state import ConversationTask, TaskParameters, task_instruction
from backend.agent.tool_execution import TurnPolicy, guarded_prompt, tool_execution_scope


class ConflictingFilterModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self):
        return 'comparison-guard-test'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            response = AIMessage(content='', tool_calls=[{
                'name': 'search_products', 'id': 'conflicting-budget',
                'args': {'query': '比较自行车', 'max_price': 999},
            }])
        else:
            response = AIMessage(content='{}')
        return ChatResult(generations=[ChatGeneration(message=response)])


class CompositionRuntimeTest(unittest.TestCase):
    def test_normal_recommendation_loads_shared_steps_once(self):
        task=self.task('推荐一款自行车');before=task.model_dump()
        with tool_execution_scope(TurnPolicy(task=task)),patch('backend.agent.tool_execution.emit_event') as emit:
            messages=guarded_prompt(read_prompt('sales')+PLAN_INSTRUCTION,
                skills=load_specialist_specs()['sales'].skills)({'messages':[HumanMessage(content=task.query)]})
        self.assertEqual(messages[0].content.count('# 商品推荐与候选选择操作说明'),1)
        self.assertNotIn('# 商品对比操作说明',messages[0].content)
        self.assertEqual([c.kwargs['skill'] for c in emit.call_args_list if c.args[0]=='skill_loaded'],['product-recommendation'])
        self.assertEqual(task.model_dump(),before)

    def test_comparison_uses_shared_steps_and_supplement_once(self):
        task=self.task()
        with tool_execution_scope(TurnPolicy(task=task)),patch('backend.agent.tool_execution.emit_event'):
            messages=guarded_prompt(read_prompt('sales')+PLAN_INSTRUCTION,
                skills=load_specialist_specs()['sales'].skills)({'messages':[HumanMessage(content=task.query)]})
        text=messages[0].content
        self.assertEqual(text.count('# 商品推荐与候选选择操作说明'),1)
        self.assertEqual(text.count('# 商品对比操作说明'),1)
        self.assertIn(PLAN_INSTRUCTION,text)

    def task(self, query='比较适合通勤的自行车'):
        return ConversationTask(kind='product_search', agent='sales', query=query,
            parameters=TaskParameters(product_category='bicycles', max_price=500))

    def test_regular_prompt_keeps_original_composition_and_message(self):
        task = self.task('推荐一款自行车')
        message = HumanMessage(content=task.query)
        base = read_prompt('sales') + PLAN_INSTRUCTION
        with tool_execution_scope(TurnPolicy(task=task)), patch('backend.agent.tool_execution.emit_event') as emit:
            messages = guarded_prompt(base, skills=('product-comparison',))({'messages': [message]})
        self.assertEqual(messages[0].content,
            base + read_prompt('execution') + task_instruction(task) + read_prompt('evidence'))
        self.assertEqual(messages[-1].content, message.content)
        self.assertFalse(any(call.args[0] == 'skill_loaded' for call in emit.call_args_list))

    def test_comparison_adds_guidance_without_mutating_task_or_messages(self):
        task = self.task()
        before = task.model_dump(mode='json')
        original = [HumanMessage(content=task.query)]
        snapshot = [m.model_dump() for m in original]
        with tool_execution_scope(TurnPolicy(task=task)), patch('backend.agent.tool_execution.emit_event') as emit:
            messages = guarded_prompt(read_prompt('sales') + PLAN_INSTRUCTION,
                skills=('product-comparison',))({'messages': original})
        self.assertIn('商品对比操作说明', messages[0].content)
        self.assertIn(PLAN_INSTRUCTION, messages[0].content)
        self.assertEqual(task.model_dump(mode='json'), before)
        self.assertEqual([m.model_dump() for m in original], snapshot)
        event = next(call for call in emit.call_args_list if call.args[0] == 'skill_loaded')
        self.assertEqual(event.kwargs['task_id'], task.task_id)
        self.assertEqual(set(event.kwargs), {'skill', 'version', 'content_sha256', 'task_id', 'revision'})

    def test_real_agent_factory_still_rejects_model_changed_budget(self):
        executions = []

        @tool('search_products')
        def search(query: str, max_price: float) -> dict:
            """Read-only test catalog search."""
            executions.append(query)
            return {'success': True, 'products': []}

        model = ConflictingFilterModel()
        roles = []
        def factory(*, role):
            roles.append(role)
            return model
        agent = build_specialist(load_specialist_specs()['sales'], [search], factory)
        with tool_execution_scope(TurnPolicy(task=self.task())), patch('backend.agent.tool_execution.emit_event'):
            result = agent.invoke({'messages': [HumanMessage(content='比较自行车，预算500以内')]})
        replies = [m for m in result['messages'] if isinstance(m, ToolMessage)]
        self.assertEqual(executions, [])
        self.assertEqual(roles, ['sales'])
        self.assertEqual(len(replies), 1)
        self.assertEqual(json.loads(replies[0].content)['error_code'], 'TASK_TOOL_NOT_ALLOWED')
        self.assertEqual(model.calls, 2)

    def test_original_input_survives_task_query_rewrite_in_real_prompt(self):
        task = self.task('推荐两款自行车')
        policy = TurnPolicy(task=task, user_query='比较两款自行车')
        with tool_execution_scope(policy), patch('backend.agent.tool_execution.emit_event'):
            messages = guarded_prompt(read_prompt('sales') + PLAN_INSTRUCTION,
                skills=('product-comparison',))({'messages': [HumanMessage(content=task.query)]})
        self.assertIn('商品对比操作说明', messages[0].content)
        self.assertEqual(task.query, '推荐两款自行车')


if __name__ == '__main__':
    unittest.main()
