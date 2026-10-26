"""Preserve genuine DeepSeek reasoning fields at the Chat Completions boundary."""
from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek


class ThinkingChatDeepSeek(ChatDeepSeek):
    """The installed adapter reads reasoning_content but omits it from requests.

    Copy only the provider's original field. Never invent reasoning for program
    messages; those are projected to evidence by context_budget instead.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(messages, stop=stop, **kwargs)
        if len(payload['messages']) != len(messages):
            raise ValueError('DeepSeek message serialization changed; refusing mismatched reasoning')
        for source, target in zip(messages, payload['messages']):
            if isinstance(source, AIMessage) and target.get('role') == 'assistant':
                reasoning = source.additional_kwargs.get('reasoning_content')
                if isinstance(reasoning, str):
                    target['reasoning_content'] = reasoning
        return payload
