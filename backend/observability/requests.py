"""One durable lifecycle for each actual provider attempt."""

import time
import uuid
from contextvars import ContextVar
from types import SimpleNamespace

from .inputs import save_input
from .scope import current


class RequestRecorder:
    def __init__(self, store, payload, config, purpose):
        self.store, self.purpose = store, purpose
        self.source = {k: v for k, v in current(store).items() if k != "request_id"}
        self.input = save_input(store, payload, config) if hasattr(store, "directory") else {}
        self.request_id = None
        self.started = None
        self.progress = set()

    def start(self, attempt):
        self.request_id = uuid.uuid4().hex
        self.last_request_id = self.request_id
        self.started = time.monotonic()
        self.progress = set()
        self.store.append(
            "request_start",
            **self.source,
            **self.input,
            request_id=self.request_id,
            purpose=self.purpose,
            attempt=attempt,
        )
        self.store.append(
            "usage", **self.source, request_id=self.request_id, purpose=self.purpose, usage=None
        )

    def delta(self, event):
        kind = event["type"]
        if kind not in self.progress:
            self.progress.add(kind)
            self.store.append(
                "request_progress",
                **self.source,
                request_id=self.request_id,
                purpose=self.purpose,
                kind=kind,
                elapsed_ms=round((time.monotonic() - self.started) * 1000, 3),
            )

    def end(self, status, *, usage=None, finish_reason=None, error_type=None, retrying=False):
        if self.request_id is None:
            if not hasattr(self, "last_request_id"):
                self.store.append(
                    "usage",
                    **self.source,
                    request_id=uuid.uuid4().hex,
                    purpose=self.purpose,
                    usage=usage,
                )
            return
        self.store.append(
            "usage", **self.source, request_id=self.request_id, purpose=self.purpose, usage=usage
        )
        self.store.append(
            "request_end",
            **self.source,
            request_id=self.request_id,
            purpose=self.purpose,
            status=status,
            finish_reason=finish_reason,
            error_type=error_type,
            retrying=retrying,
            duration_ms=round((time.monotonic() - self.started) * 1000, 3),
        )
        self.request_id = None


# Provider retries report their outcome here; lightweight test providers use the
# same wrapper and therefore produce the same evidence contract.
_recorder = ContextVar("request_recorder", default=None)


def current_recorder():
    return _recorder.get()


def request_payload(config, messages, tools):
    from urllib.parse import urlparse

    native = (
        urlparse(config.base_url).hostname == "api.openai.com"
        or config.catalog_provider == "openai"
    )
    payload = {
        "model": config.model,
        "messages": messages,
        "stream": True,
        "max_completion_tokens" if native else "max_tokens": config.max_output_tokens,
    }
    from backend.models.reasoning import request_settings

    payload.update(request_settings(config))
    if tools:
        payload["tools"] = tools
    return payload


async def recorded_request(
    provider, store, messages, tools, on_delta, on_attempt=None, purpose="main", config=None
):
    config = (
        config
        or getattr(provider, "config", None)
        or SimpleNamespace(
            model="unknown",
            base_url="",
            catalog_provider="",
            max_output_tokens=0,
        )
    )
    recorder = RequestRecorder(store, request_payload(config, messages, tools), config, purpose)
    token = _recorder.set(recorder)

    async def attempt(number):
        if on_attempt:
            await on_attempt(number)
        recorder.start(number)

    async def delta(event):
        recorder.delta(event)
        await on_delta({**event, "request_id": recorder.request_id})

    try:
        response = await provider.complete(
            messages,
            tools,
            on_delta=delta,
            on_attempt=attempt,
            **({"purpose": purpose} if purpose != "main" else {}),
        )
        recorder.end("completed", usage=response.usage, finish_reason=response.finish_reason)
        response.request_id = getattr(recorder, "last_request_id", None)
        return response
    except BaseException as exc:
        import asyncio

        recorder.end(
            "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
            usage=getattr(exc, "usage", None),
            error_type=type(exc).__name__,
        )
        raise
    finally:
        _recorder.reset(token)
