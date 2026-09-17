"""Explicitly retried, cancellable OpenAI-compatible SSE transport."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from config import Config
from conversation.streaming import StreamAccumulator, sse_events


@dataclass
class Response:
    content: str
    tool_calls: list[dict]
    finish_reason: str
    usage: dict | None
    reasoning_content: str = ""


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

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        on_delta: Callable[[dict], Awaitable[None]],
        on_attempt: Callable[[int], Awaitable[None]] | None = None,
        purpose: str = "main",
    ) -> Response:
        payload = {
            "model": self.config.model,
            "messages": messages,
            "stream": True,
            "max_tokens": self.config.max_output_tokens,
        }
        if tools:
            payload["tools"] = tools
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
                        return Response(
                            state.content,
                            calls,
                            state.finish_reason,
                            state.usage,
                            state.reasoning_content,
                        )
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
                if not error.retryable or state.received_output or attempt == 3:
                    raise error from None
                await asyncio.sleep(self.retry_delay * attempt)
        raise AssertionError("unreachable")
