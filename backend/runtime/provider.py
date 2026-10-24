"""Explicitly retried, cancellable OpenAI-compatible SSE transport."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from backend.config import Config
from backend.observability.requests import current_recorder, request_payload
from backend.runtime.streaming import StreamAccumulator, sse_events


@dataclass
class Response:
    content: str
    tool_calls: list[dict]
    finish_reason: str
    usage: dict | None
    reasoning_content: str = ""
    request_id: str | None = None


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        partial_content: str = "",
        received_output: bool = False,
        retryable: bool = False,
        reasoning_content: str = "",
        tool_calls=None,
    ):
        super().__init__(message)
        self.partial_content = partial_content
        self.received_output = received_output
        self.retryable = retryable
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls or []
        self.usage = None


class Provider:
    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_delay: float = 0.5,
    ):
        self.config = config
        self.transport = transport
        self.retry_delay = retry_delay

    async def count_tokens(self, messages, tools):
        """Ask BigModel's tokenizer for this exact model, messages and tools.

        None means no supported counting endpoint; it is never a token count.
        A supported endpoint failing must stop preparation rather than silently
        changing the units used by the compaction threshold.
        """
        endpoint = urlparse(self.config.base_url)
        if not (
            endpoint.hostname == "open.bigmodel.cn"
            and endpoint.path.rstrip("/") == "/api/paas/v4"
            and self.config.model.casefold().startswith("glm-")
        ):
            return None
        payload = {"model": self.config.model, "messages": messages}
        if tools:
            payload["tools"] = tools
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.config.request_timeout
            ) as client:
                response = await client.post(
                    self.config.base_url.rstrip("/") + "/tokenizer",
                    json=payload,
                    headers={"Authorization": "Bearer " + self.config.api_key},
                )
                response.raise_for_status()
                count = response.json()["usage"]["prompt_tokens"]
                if type(count) is not int or count < 0:
                    raise ValueError("Invalid token count")
                return count
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            raise ProviderError("模型服务 token 计数失败；未使用字节估算替代真实计数。") from None

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        on_delta: Callable[[dict], Awaitable[None]],
        on_attempt: Callable[[int], Awaitable[None]] | None = None,
        purpose: str = "main",
    ) -> Response:
        payload = request_payload(self.config, messages, tools)
        recorder = current_recorder()
        for attempt in range(1, 4):
            if on_attempt:
                await on_attempt(attempt)
            state = StreamAccumulator()
            try:
                async with httpx.AsyncClient(
                    transport=self.transport,  # noqa: SIM117
                    timeout=self.config.request_timeout,
                ) as client:
                    async with client.stream(
                        "POST",
                        self.config.base_url.rstrip("/") + "/chat/completions",
                        json=payload,
                        headers={"Authorization": "Bearer " + self.config.api_key},
                    ) as response:
                        if response.status_code >= 400:
                            raise ProviderError(
                                f"模型服务 HTTP {response.status_code}；请检查配置或稍后重试。",
                                retryable=response.status_code in {408, 429, 500, 502, 503, 504},
                            )
                        done = False
                        async for data in sse_events(response.aiter_lines()):
                            if data == "[DONE]":
                                done = True
                                break
                            chunk = json.loads(data)
                            if "error" in chunk:
                                raise ValueError("Model stream reported an error")
                            for event in state.add(chunk):
                                await on_delta(event)
                        if not done or state.finish_reason is None:
                            raise ProviderError(
                                "模型流未完整结束，已保存部分输出。", retryable=True
                            )
                        calls = state.tool_calls()
                        if recorder:
                            recorder.end(
                                "completed", usage=state.usage, finish_reason=state.finish_reason
                            )
                        return Response(
                            state.content,
                            calls,
                            state.finish_reason,
                            state.usage,
                            state.reasoning_content,
                        )
            except asyncio.CancelledError:
                if recorder:
                    recorder.end("cancelled", usage=state.usage, error_type="user_cancelled")
                raise
            except (httpx.HTTPError, ValueError, TypeError, KeyError, ProviderError) as exc:
                error = (
                    exc
                    if isinstance(exc, ProviderError)
                    else ProviderError(
                        "模型网络连接失败。"
                        if isinstance(exc, httpx.HTTPError)
                        else "模型响应格式无效。",
                        retryable=isinstance(exc, httpx.HTTPError),
                    )
                )
                error.partial_content = state.content
                error.reasoning_content = state.reasoning_content
                error.tool_calls = list(state.calls.values())
                error.received_output = state.received_output
                error.usage = state.usage
                if recorder:
                    recorder.end(
                        "failed",
                        usage=state.usage,
                        error_type=type(exc).__name__,
                        retrying=error.retryable and not state.received_output and attempt < 3,
                    )
                if not error.retryable or state.received_output or attempt == 3:
                    raise error from None
                await asyncio.sleep(self.retry_delay * attempt)
            except Exception as exc:
                if recorder:
                    recorder.end("failed", usage=state.usage, error_type=type(exc).__name__)
                raise
        raise AssertionError("unreachable")
