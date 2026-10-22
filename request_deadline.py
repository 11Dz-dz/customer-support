"""One monotonic execution deadline; cancel native async model I/O, never abandon write threads."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from contextvars import ContextVar
import math
import os
import threading
import time
from functools import wraps

_deadline = ContextVar('customer_service_request_deadline', default=None)
_loop = None
_loop_lock = threading.Lock()


class RequestDeadlineExceeded(TimeoutError):
    pass


class ModelCallTimeout(TimeoutError):
    pass


def timeout_setting():
    value = float(os.getenv('REQUEST_TIMEOUT_SECONDS', '90'))
    if not math.isfinite(value) or not 1 <= value <= 300:
        raise ValueError('REQUEST_TIMEOUT_SECONDS must be between 1 and 300')
    return value


def deadline_from_config(config):
    value = (config or {}).get('metadata', {}).get('request_deadline_monotonic')
    return float(value) if value is not None else time.monotonic() + timeout_setting()


@contextmanager
def deadline_scope(value):
    token = _deadline.set(value)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_seconds(cap=None):
    value = _deadline.get()
    remaining = None if value is None else max(0.0, value - time.monotonic())
    return cap if remaining is None else remaining if cap is None else min(cap, remaining)


def check_deadline():
    if remaining_seconds() == 0:
        raise RequestDeadlineExceeded('request execution deadline exceeded')


def execution_deadline(function):
    @wraps(function)
    def wrapped(state, config):
        with deadline_scope(deadline_from_config(config)):
            return function(state, config)
    return wrapped


def _model_loop():
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name='model-io-loop', daemon=True).start()
        return _loop


def invoke_model(runnable, messages, config=None, *, cap=60.0):
    """Bound the whole native async call, including streamed chunks, not just socket inactivity.

    Only model I/O lives on this loop. No graph execution or business write is submitted
    to an abandoned worker. Cancellation stops consuming the response; provider-side
    generation/billing may already have occurred.
    """
    from chat_events import emit_event
    from langchain_core.runnables import Runnable
    check_deadline()
    seconds = remaining_seconds(cap)
    # Unit-test adapters and non-model test doubles remain synchronous.
    if not isinstance(runnable, Runnable):
        result = runnable.invoke(messages, config)
        check_deadline()
        return result

    async def run():
        return await asyncio.wait_for(runnable.ainvoke(messages, config), timeout=seconds)

    future = asyncio.run_coroutine_threadsafe(run(), _model_loop())
    try:
        return future.result(timeout=seconds + 0.1)
    except FutureTimeout:
        future.cancel()
        exhausted = remaining_seconds() == 0
        emit_event('model_timeout', config, request_exhausted=exhausted)
        exception = RequestDeadlineExceeded if exhausted else ModelCallTimeout
        raise exception('model response deadline exceeded') from None
    except BaseException:
        future.cancel()
        raise


def bounded_model(runnable):
    from langchain_core.runnables import RunnableLambda
    return RunnableLambda(lambda messages, config: invoke_model(runnable, messages, config),
                          name='deadline_model')
