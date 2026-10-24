import copy
import json
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
            content=json.dumps({
                "constraints": [], "decisions": [], "findings": ["Edited a.py:10"],
                "next_steps": [], "relevant_files": ["a.py"], "references": ["outputs/x.txt"],
                "open_questions": [], "rejected_approaches": [],
            }),
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


async def test_recovered_tool_status_is_explicit_in_prepared_context(tmp_path):
    from backend.conversation.manager import ConversationManager
    from backend.conversation.resume import recover_session
    from backend.conversation.store import SessionStore

    store = SessionStore(tmp_path)
    store.append("turn_start", turn_id="turn")
    calls = [{"id": cid, "type": "function", "function": {
        "name": "Shell", "arguments": "{}",
    }} for cid in ("done", "uncertain", "not_started")]
    store.append("message", turn_id="turn", request_id="request", message_id="a",
                 role="assistant", content="", tool_calls=calls)
    store.append("tool_result", turn_id="turn", request_id="request", call_id="done",
                 status="succeeded", content="confirmed output")
    for cid in ("uncertain", "not_started"):
        store.append("tool_start", turn_id="turn", request_id="request", call_id=cid, name="Shell")
    store.append("tool_running", turn_id="turn", request_id="request", call_id="uncertain")
    restored = recover_session(SessionStore(tmp_path, store.session_id))
    before = copy.deepcopy(restored.events)
    manager = ContextManager(SimpleNamespace(context_window=8000, max_output_tokens=1000),
                             Provider(), restored)
    result = await manager.prepare(ConversationManager(restored).messages(), [])
    tools = {m["tool_call_id"]: m["content"] for m in result if m["role"] == "tool"}
    assert "UNKNOWN" in tools["uncertain"]
    assert json.loads(tools["uncertain"])["status"] == "unknown"
    assert json.loads(tools["not_started"])["status"] == "cancelled"
    assert tools["done"] == "confirmed output"
    assert restored.events[:len(before)] == before


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


@pytest.mark.parametrize("structured", [False, True])
async def test_compressed_history_restores_from_journal_without_new_summary(tmp_path, structured):
    from backend.context.compation_ckeckpoint import CompactionCheckpoint
    from backend.conversation.store import SessionStore

    store = SessionStore(tmp_path)
    body = [{"role": "user", "content": "old task"}, {"role": "assistant", "content": "old answer"}]
    prepared = [{"role": "user", "content": "Historical summary: old task done"}]
    if structured:
        # Previously saved six-field summaries remain usable without regeneration.
        prepared[0]["content"] = "Historical summary:\n" + json.dumps({"history_summary": {
            "constraints": [], "decisions": [], "findings": ["old answer"],
            "next_steps": [], "relevant_files": [], "references": [],
        }})
    CompactionCheckpoint(store).save(body, prepared, 100)
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
        CompactionCheckpoint(restored_store).restore([{"role": "user", "content": "changed"}]) is None
    )


async def test_compaction_does_not_inject_duplicate_task_state(tmp_path):
    from backend.conversation.store import SessionStore

    store = SessionStore(tmp_path)
    provider = Provider()
    config = SimpleNamespace(context_window=8000, max_output_tokens=1000)
    messages = history()
    result = await ContextManager(config, provider, store).prepare(messages, [])
    assert provider.requests
    assert result[0] == messages[0]
    assert all('"runtime_state"' not in str(request) for request in provider.requests)
    assert any(m.get("content", "").startswith("Historical summary") for m in result)
    reopened = SessionStore(tmp_path, store.session_id)
    restored = await ContextManager(config, Provider(True), reopened).prepare(messages, [])
    assert restored == result
    assert not any(e["type"] in {"checkpoint", "task_state"} for e in store.events)


@pytest.mark.parametrize("invalid", [
    "plain text", "[]", '{"status":"completed","verification_status":"passed"}',
] + [
    json.dumps({
        "constraints": [], "decisions": [], "findings": invalid_findings,
        "next_steps": [], "relevant_files": [], "references": [],
        "open_questions": [], "rejected_approaches": [],
    }) for invalid_findings in ({}, [{"status": "passed"}])
] + [
    json.dumps({
        "constraints": [], "decisions": [], "findings": [], "next_steps": [],
        "relevant_files": [], "references": [], "open_questions": questions,
        "rejected_approaches": rejected,
    }) for questions, rejected in [("unknown", []), ([], [False])]
])
async def test_invalid_structured_summary_retains_original_history(invalid):
    class InvalidProvider(Provider):
        async def complete(self, messages, tools, **kwargs):
            result = await super().complete(messages, tools, **kwargs)
            result.content = invalid
            return result

    messages = history()
    store = Store()
    manager = ContextManager(
        SimpleNamespace(context_window=estimate(messages) + 1200, max_output_tokens=1000),
        InvalidProvider(), store,
    )
    assert await manager.prepare(messages, []) == messages
    assert any(e["type"] == "context_compression_error" for e in store.events)
    assert not any(e["type"] == "context_compressed" for e in store.events)


async def test_long_objective_is_present_once_without_task_state(tmp_path):
    from backend.conversation.store import SessionStore

    store = SessionStore(tmp_path)
    objective = "x" * 4000
    messages = [{"role": "user", "content": objective}]
    manager = ContextManager(
        SimpleNamespace(context_window=7000, max_output_tokens=1000), Provider(True), store,
    )
    assert await manager.prepare(messages, []) == messages
