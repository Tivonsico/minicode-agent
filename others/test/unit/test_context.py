import copy
from types import SimpleNamespace

import pytest

from backend.context import ContextManager, ContextOverflow
from backend.context.budget import estimate


class Store:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append({"type": event_type, **data})


class Provider:
    def __init__(self, fail=False):
        self.fail, self.requests = fail, []

    async def complete(self, messages, tools, **kwargs):
        self.requests.append(messages)
        if self.fail:
            raise RuntimeError("secret must not be journaled")
        return SimpleNamespace(
            content="Earlier task edited a.py:10; log at outputs/x.txt",
            finish_reason="stop",
            tool_calls=[],
            usage=None,
        )


def history():
    return (
        [{"role": "system", "content": "policy"}]
        + [
            {"role": "user" if i % 2 == 0 else "assistant", "content": str(i) + "x" * 1000}
            for i in range(10)
        ]
        + [{"role": "user", "content": "CURRENT TASK"}]
    )


@pytest.mark.asyncio
async def test_failed_compression_preserves_input_if_it_fits():
    messages = history()
    before = copy.deepcopy(messages)
    size = estimate(messages)
    store = Store()
    manager = ContextManager(
        SimpleNamespace(context_window=size + 1200, max_output_tokens=1000), Provider(True), store
    )
    assert await manager.prepare(messages, []) == before
    assert messages == before
    assert all("secret" not in str(e) for e in store.events)
    assert any(e["type"] == "usage" and e["usage"] is None for e in store.events)


@pytest.mark.asyncio
async def test_failed_compression_overflow_pauses_without_mutation():
    messages = history()
    before = copy.deepcopy(messages)
    manager = ContextManager(
        SimpleNamespace(context_window=7000, max_output_tokens=1000), Provider(True), Store()
    )
    with pytest.raises(ContextOverflow):
        await manager.prepare(messages, [])
    assert messages == before


@pytest.mark.asyncio
async def test_compression_preserves_recent_tool_groups_and_is_reused():
    messages = history() + [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call",
                    "type": "function",
                    "function": {"name": "ReadFile", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call", "content": "result"},
    ]
    provider = Provider()
    manager = ContextManager(
        SimpleNamespace(context_window=8000, max_output_tokens=1000), provider, Store()
    )
    result = await manager.prepare(messages, [])
    assert result[0] == messages[0]
    assert result[-3:] == messages[-3:]
    assert all(estimate(r) <= 7000 for r in provider.requests)
    requests = len(provider.requests)
    assert await manager.prepare(messages, []) == result
    assert len(provider.requests) == requests


@pytest.mark.asyncio
async def test_tool_schemas_count_toward_budget():
    manager = ContextManager(
        SimpleNamespace(context_window=2000, max_output_tokens=1000), Provider(), Store()
    )
    with pytest.raises(ContextOverflow):
        await manager.prepare([{"role": "user", "content": "hello"}], [{"description": "x" * 1500}])


@pytest.mark.asyncio
async def test_single_long_round_compacts_old_tools_preserves_task():
    messages = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "Keep this current task"},
    ]
    for i in range(10):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": str(i),
                            "type": "function",
                            "function": {"name": "ReadFile", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": str(i), "content": "x" * 1000},
            ]
        )
    provider = Provider()
    manager = ContextManager(
        SimpleNamespace(context_window=8000, max_output_tokens=1000), provider, Store()
    )
    original = copy.deepcopy(messages)
    result = await manager.prepare(messages, [])
    assert {"role": "user", "content": "Keep this current task"} in result
    assert result[-8:] == messages[-8:]
    assert messages == original
    assert manager.last_estimate <= 7000
    requests = len(provider.requests)
    assert await manager.prepare(messages, []) == result
    assert len(provider.requests) == requests


async def test_compressed_history_restores_from_journal_without_new_summary(tmp_path):
    from backend.context.checkpoint import ContextCheckpoint
    from backend.conversation.store import SessionStore

    store = SessionStore(tmp_path)
    body = [{"role": "user", "content": "old task"}, {"role": "assistant", "content": "old answer"}]
    prepared = [{"role": "user", "content": "Historical summary: old task done"}]
    ContextCheckpoint(store).save(body, prepared, 100)
    provider = Provider()
    restored_store = SessionStore(tmp_path, store.session_id)
    manager = ContextManager(
        SimpleNamespace(context_window=8000, max_output_tokens=1000), provider, restored_store
    )
    result = await manager.prepare(
        [{"role": "system", "content": "new system rules"}]
        + body
        + [{"role": "user", "content": "next"}],
        [],
    )
    assert result == [{"role": "system", "content": "new system rules"}] + prepared + [
        {"role": "user", "content": "next"}
    ]
    assert provider.requests == []
    assert (
        ContextCheckpoint(restored_store).restore([{"role": "user", "content": "changed"}]) is None
    )
