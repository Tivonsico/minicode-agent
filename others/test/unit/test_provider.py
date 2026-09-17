import asyncio
import json

import httpx
import pytest

from config import Config
from conversation.provider import Provider, ProviderError


def chunk(delta=None, finish=None, **extra):
    return (
        "data: "
        + json.dumps(
            {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **extra}
        )
        + "\n\n"
    )


def provider(tmp_path, handler):
    return Provider(
        Config(tmp_path, "private-key", context_window=10000, max_output_tokens=1000),
        transport=httpx.MockTransport(handler),
        retry_delay=0,
    )


async def noop(event):
    pass


async def test_multitool_fragments_roles_usage_and_reasoning(tmp_path):
    events = []
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}]

    async def callback(event):
        events.append(event)

    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "GLM-5.3-Flash"
        assert body["messages"] == messages
        assert "thinking" not in body and "temperature" not in body
        data = chunk({"reasoning_content": "reason"}) + chunk(
            {
                "content": "working",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "a",
                        "function": {"name": "ReadFile", "arguments": '{"path":'},
                    },
                    {
                        "index": 1,
                        "id": "b",
                        "function": {"name": "Grep", "arguments": '{"pattern":'},
                    },
                ],
            }
        )
        data += chunk(
            {
                "tool_calls": [
                    {"index": 1, "function": {"arguments": '"foo"}'}},
                    {"index": 0, "function": {"arguments": '"a.txt"}'}},
                ]
            },
            "tool_calls",
        )
        data += 'data: {"choices":[],"usage":{"total_tokens":42}}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=data)

    response = await provider(tmp_path, handler).complete(messages, [], callback)
    assert response.reasoning_content == "reason" and response.content == "working"
    assert response.tool_calls[0]["function"]["arguments"] == '{"path":"a.txt"}'
    assert response.tool_calls[1]["id"] == "b"
    assert response.usage == {"total_tokens": 42}
    assert [e["type"] for e in events] == ["reasoning", "text", "tool_delta", "tool_delta"]


@pytest.mark.parametrize(
    "delta",
    [
        {"content": "partial"},
        {"reasoning_content": "partial"},
        {"tool_calls": [{"index": 0, "id": "a", "function": {"arguments": "{"}}]},
    ],
)
async def test_output_then_disconnect_never_retries(tmp_path, delta):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(200, text=chunk(delta))

    with pytest.raises(ProviderError) as error:
        await provider(tmp_path, handler).complete([], [], noop)
    assert error.value.received_output
    assert len(attempts) == 1


async def test_no_output_retry_count_and_missing_usage(tmp_path):
    attempts = []
    callbacks = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(429)
        return httpx.Response(200, text=chunk({"content": "ok"}, "stop") + "data: [DONE]\n\n")

    async def on_attempt(number):
        callbacks.append(number)

    result = await provider(tmp_path, handler).complete([], [], noop, on_attempt)
    assert callbacks == [1, 2, 3] and result.usage is None


async def test_auth_failure_never_retries_or_leaks_body(tmp_path):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(401, text="private-key")

    with pytest.raises(ProviderError) as error:
        await provider(tmp_path, handler).complete([], [], noop)
    assert len(attempts) == 1 and "private-key" not in str(error.value)


async def test_incomplete_arguments_and_missing_finish_rejected(tmp_path):
    for data in (
        chunk({"content": "hello"}) + "data: [DONE]\n\n",
        chunk(
            {
                "tool_calls": [
                    {"index": 0, "id": "a", "function": {"name": "WriteFile", "arguments": "{"}}
                ]
            },
            "tool_calls",
        )
        + "data: [DONE]\n\n",
    ):
        with pytest.raises(ProviderError):
            await provider(tmp_path, lambda r, data=data: httpx.Response(200, text=data)).complete(
                [], [], noop
            )


async def test_cancellation_closes_transport(tmp_path):
    started, closed = asyncio.Event(), asyncio.Event()

    class BlockingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield chunk({"content": "partial"}).encode()
            started.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    instance = provider(tmp_path, lambda r: httpx.Response(200, stream=BlockingStream()))
    task = asyncio.create_task(instance.complete([], [], noop))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
