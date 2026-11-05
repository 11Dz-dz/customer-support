"""Opt-in LangSmith request traces, correlated with local events; no public trace IDs."""
import atexit
from contextlib import ExitStack,contextmanager
from contextvars import ContextVar
from datetime import datetime,timezone
import logging
import json
import os
import re
import threading
from urllib.parse import urlsplit,unquote
from uuid import UUID

from langsmith import Client,trace,tracing_context
from langsmith.run_helpers import get_current_run_tree
from langsmith.run_trees import AgentAddress
from langchain_core.tracers.langchain import LangChainTracer

logger=logging.getLogger(__name__)
_request_metadata=ContextVar('trace_request_metadata',default={})
_active=ContextVar('customer_support_trace',default=None)
_client=None
_client_settings=None
_client_lock=threading.Lock()
_upload_errors=0
_SECRET_KEYS={'password','passwd','api_key','authorization','cookie','set_cookie','csrf_token',
              'token_hash','access_token','refresh_token','secret','reasoning_content','reasoning',
              'thinking','signature','headers','repr','request_deadline_monotonic'}
_KEY_PATTERN=re.compile(r'\b(?:sk-[A-Za-z0-9_-]{12,}|lsv2_[A-Za-z0-9_-]{12,})\b')


def enabled(value):return str(value or '').lower() in {'true','1','yes','on'}


def settings():
    return {'enabled':enabled(os.getenv('LANGSMITH_TRACING')),
            'api_key':os.getenv('LANGSMITH_API_KEY','').strip(),
            'endpoint':os.getenv('LANGSMITH_ENDPOINT','https://api.smith.langchain.com').strip(),
            'project':os.getenv('LANGSMITH_PROJECT','customer-support').strip() or 'customer-support',
            'workspace_id':os.getenv('LANGSMITH_WORKSPACE_ID','').strip() or None,
            'agent_id':os.getenv('LANGSMITH_AGENT_ID','').strip() or None,
            'agent_environment':os.getenv('LANGSMITH_AGENT_ENVIRONMENT','development').strip(),
            'hide_inputs':enabled(os.getenv('LANGSMITH_HIDE_INPUTS')),
            'hide_outputs':enabled(os.getenv('LANGSMITH_HIDE_OUTPUTS'))}


def secret_values():
    values=[]
    for key,value in os.environ.items():
        if any(word in key.upper() for word in ('API_KEY','PASSWORD','SECRET','TOKEN','DATABASE_URL')) and len(value)>=4:
            values.append(value)
            if 'DATABASE_URL' in key.upper():
                try:
                    password=unquote(urlsplit(value).password or '')
                    if len(password)>=4:values.append(password)
                except ValueError:pass
    return sorted(set(values),key=len,reverse=True)


def redact_trace_data(value,secrets=None,_depth=0):
    """Transform an outbound copy, never the model's actual inputs or checkpoints.

    This masks configured credentials and known secret fields, not arbitrary business PII.
    """
    secrets=secret_values() if secrets is None else secrets
    if _depth>64:return '[DEPTH_LIMIT]'
    if isinstance(value,str):
        if value.lstrip().startswith(('{','[')):
            try:
                parsed=json.loads(value)
                if isinstance(parsed,(dict,list)):
                    return json.dumps(redact_trace_data(parsed,secrets,_depth+1),ensure_ascii=False)
            except (ValueError,RecursionError):pass
        for secret in secrets:value=value.replace(secret,'[REDACTED]')
        return _KEY_PATTERN.sub('[REDACTED]',value)
    if isinstance(value,dict):
        return {str(k):('[REDACTED]' if str(k).lower().replace('-','_') in _SECRET_KEYS
                        or str(k).lower().endswith('_api_key') else
                        redact_trace_data(v,secrets,_depth+1)) for k,v in value.items()}
    if isinstance(value,(tuple,list)):
        return [redact_trace_data(v,secrets,_depth+1) for v in value]
    if hasattr(value,'model_dump'):
        return redact_trace_data(value.model_dump(),secrets,_depth+1)
    return value


def upload_error(exc):
    global _upload_errors
    _upload_errors+=1
    # SDK exceptions may include request content; local logs retain only a safe type.
    logger.warning('langsmith_upload_failed error_type=%s',type(exc).__name__)


def make_client(config,**overrides):
    parts=urlsplit(config['endpoint'])
    if parts.scheme not in {'http','https'} or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('LANGSMITH_ENDPOINT must be an API origin or base path, without credentials/query')
    secrets=secret_values()
    redact=lambda data:redact_trace_data(data,secrets)
    return Client(api_key=config['api_key'],api_url=config['endpoint'],workspace_id=config['workspace_id'],
        timeout_ms=(2000,5000),hide_inputs=True if config['hide_inputs'] else redact,
        hide_outputs=True if config['hide_outputs'] else redact,hide_metadata=redact,
        process_buffered_run_ops=lambda ops:[redact(op) for op in ops],
        run_ops_buffer_size=1,run_ops_buffer_timeout_ms=500,
        tracing_error_callback=upload_error,**overrides)


def get_client(config):
    global _client,_client_settings
    with _client_lock:
        if _client is None:
            _client=make_client(config);_client_settings=dict(config)
        elif _client_settings!=config:
            # Configuration is process-scoped; changing .env requires a normal restart.
            raise ValueError('Tracing configuration changed; restart service')
        return _client


def flush_traces():
    if _client is not None:
        try:_client.flush(timeout=5)
        except Exception as exc:upload_error(exc)


atexit.register(flush_traces)


@contextmanager
def request_metadata_scope(**fields):
    allowed={k:str(v) for k,v in fields.items() if k in {
        'web_run_id','entrypoint','eval_batch_id','eval_case_id','eval_repeat',
        'eval_turn','eval_dataset_version'}}
    token=_request_metadata.set(allowed)
    try:yield
    finally:_request_metadata.reset(token)


def request_metadata():return dict(_request_metadata.get())


class TraceTurn:
    def __init__(self,root=None):
        self.root=root;self.outputs={};self.error=None;self.count=0
        self.lock=threading.Lock()

    def finish(self,answer):
        self.outputs['answer']=answer


@contextmanager
def trace_turn(config,query,mode):
    """One root per invocation, including streamed invocations and independent users."""
    config['run_name']='customer_support.graph'
    config['metadata'].update(request_metadata())
    config['metadata'].setdefault('entrypoint','cli')
    config['metadata']['mode']=mode
    stack=ExitStack();handle=TraceTurn()
    cfg=settings()
    if cfg['enabled'] and cfg['api_key']:
        try:
            client=get_client(cfg)
            root_id=UUID(config['metadata']['request_id'])
            config['metadata']['langsmith_trace_id']=str(root_id)
            metadata={**config['metadata'],'session_id':config['metadata']['thread_id']}
            address=AgentAddress(id=cfg['agent_id'],environment=cfg['agent_environment']) if cfg['agent_id'] else None
            stack.enter_context(tracing_context(enabled=True,client=client,project_name=cfg['project'],address=address,
                metadata=metadata,tags=['customer-support',metadata['entrypoint']]))
            root=stack.enter_context(trace('customer_support.turn',inputs={'message':query},
                run_id=root_id,client=client,project_name=cfg['project'],address=address,metadata=metadata))
            handle=TraceTurn(root)
            # Explicit client ensures automatic child spans use the same redaction policy.
            config['callbacks']=[*(config.get('callbacks') or []),LangChainTracer(client=client,project_name=cfg['project'])]
        except Exception as exc:
            try:stack.close()
            except Exception:pass
            stack=ExitStack();config['metadata'].pop('langsmith_trace_id',None)
            logger.warning('langsmith_setup_failed error_type=%s',type(exc).__name__)
            handle=TraceTurn()
    elif cfg['enabled']:
        logger.warning('langsmith_disabled reason=MISSING_API_KEY')
    if handle.root is None:
        # Prevent an implicit unsanitized default tracer when credentials are missing/invalid.
        stack.enter_context(tracing_context(enabled=False))
    token=_active.set(handle)
    try:yield handle
    except BaseException as exc:
        handle.error=type(exc).__name__
        raise
    finally:
        _active.reset(token)
        if handle.root is not None:
            try:handle.root.end(outputs=handle.outputs,error=handle.error)
            except Exception as exc:upload_error(exc)
        try:stack.close()
        except Exception as exc:upload_error(exc)


def record_trace_event(event):
    handle=_active.get()
    if handle is None or handle.root is None or event.get('event')=='verified_answer_chunk':return
    try:
        with handle.lock:
            if handle.count>=500:return
            handle.count+=1
            if event.get('event')=='task_end':
                tasks=handle.outputs.setdefault('task_results',{})
                tasks[str(event.get('task_id'))]={k:event[k] for k in ('task_type','status','evidence_count','failure_code') if k in event}
            current=get_current_run_tree()
            target=current if current is not None and current.trace_id==handle.root.trace_id else handle.root
            target.add_event({'name':event['event'],'time':datetime.now(timezone.utc),
                              'kwargs':redact_trace_data(event)})
    except Exception as exc:upload_error(exc)
