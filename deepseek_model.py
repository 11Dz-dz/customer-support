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


def provider_settings():
    """Explicit provider/endpoint; never try a credential against other providers."""
    import os
    from urllib.parse import urlparse
    provider=os.getenv('DEEPSEEK_PROVIDER','deepseek').strip().lower()
    if provider not in {'deepseek','bailian'}:raise ValueError('DEEPSEEK_PROVIDER must be deepseek or bailian')
    default=('https://dashscope.aliyuncs.com/compatible-mode/v1' if provider=='bailian' else 'https://api.deepseek.com')
    base=os.getenv('DEEPSEEK_BASE_URL',default).strip().rstrip('/')
    parsed=urlparse(base)
    allowed=(parsed.hostname=='api.deepseek.com' if provider=='deepseek' else
             parsed.hostname in {'dashscope.aliyuncs.com','dashscope-intl.aliyuncs.com','dashscope-us.aliyuncs.com'} or
             bool(parsed.hostname and parsed.hostname.endswith('.maas.aliyuncs.com')))
    if parsed.scheme!='https' or not allowed or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Model endpoint must be an HTTPS endpoint of the configured provider')
    model=os.getenv('DEEPSEEK_MODEL','deepseek-v4.1-flash-1017' if provider=='bailian' else 'deepseek-flash').strip()
    return {'provider':provider,'base_url':base,'model':model}


def provider_body(role):
    # Model planning and tool selection use non-thinking for bounded response time.
    # Genuine provider reasoning fields remain supported by the adapter above.
    settings=provider_settings()
    return {'enable_thinking':False} if settings['provider']=='bailian' else {'thinking':{'type':'disabled'}}
