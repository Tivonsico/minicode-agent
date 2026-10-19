"""A dropped stream cannot dispatch partial tools or replay prior side effects."""

from backend.runtime.provider import ProviderError, Response
from test.integration.test_agent_loop import FakeExecutor, make_loop
from test.support.tooling import call


class DroppedProvider:
    def __init__(self, *, repeated=False):
        self.calls = 0
        self.messages = []
        self.repeated = repeated

    async def complete(self, messages, tools, on_delta, on_attempt):
        await on_attempt(1)
        self.calls += 1
        self.messages.append(messages)
        if self.calls == 1:
            return Response(
                "",
                [call("WriteFile", {"path": "effect.txt", "content": "once"}, "committed")],
                "tool_calls",
                None,
            )
        if self.calls == 2 or self.repeated:
            await on_delta({"type": "text", "content": "UNCOMMITTED_FRAGMENT"})
            raise ProviderError(
                "dropped",
                received_output=True,
                retryable=True,
                tool_calls=[{"id": "uncommitted", "function": {"name": "WriteFile"}}],
            )
        return Response("done", [], "stop", None)


async def test_partial_stream_recovery_keeps_history_and_does_not_replay(tmp_path):
    provider = DroppedProvider()
    executor = FakeExecutor()
    loop, store, _ = make_loop(tmp_path, provider, executor)
    assert await loop.run("inspect") == "completed"
    assert [call["id"] for call in executor.calls] == ["committed"]
    assert provider.calls == loop.requests == 3
    assert "UNCOMMITTED_FRAGMENT" not in str(provider.messages[2])
    assert (
        len([e for e in store.events if e["type"] == "request_end" and e["status"] == "failed"])
        == 1
    )
    recovery = next(e for e in store.events if e["type"] == "request_recovery")
    assert recovery["request_id"] and recovery["discarded_tool_calls"] == 1
    assert len([e for e in store.events if e["type"] == "message" and e["role"] == "user"]) == 1


async def test_discarded_stream_stays_out_of_context_after_restart(tmp_path):
    from backend.conversation.projection import project_messages
    from backend.conversation.resume import recover_session
    from backend.conversation.store import SessionStore

    executor = FakeExecutor()
    loop, store, _ = make_loop(tmp_path, DroppedProvider(), executor)
    assert await loop.run("inspect") == "completed"
    before = project_messages(store.events)
    reopened = SessionStore(tmp_path, store.session_id)
    recover_session(reopened)
    assert project_messages(reopened.events) == before
    assert [item["id"] for item in executor.calls] == ["committed"]


async def test_partial_stream_recovery_is_bounded(tmp_path):
    provider = DroppedProvider(repeated=True)
    loop, store, _ = make_loop(tmp_path, provider)
    assert await loop.run("inspect") == "paused"
    assert provider.calls == 3
    assert len([e for e in store.events if e["type"] == "request_recovery"]) == 1


async def test_partial_stream_cannot_bypass_request_ceiling(tmp_path):
    class BudgetProvider(DroppedProvider):
        async def complete(self, messages, tools, on_delta, on_attempt):
            for _ in range(30):
                await on_attempt(1)
            await on_delta({"type": "text", "content": "partial"})
            raise ProviderError("drop", received_output=True, retryable=True)

    loop, store, _ = make_loop(tmp_path, BudgetProvider())
    assert await loop.run("inspect") == "paused"
    assert loop.requests == 30
    assert not any(e["type"] == "request_recovery" for e in store.events)
