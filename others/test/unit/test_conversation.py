import json

import pytest

from backend.conversation.manager import ConversationManager
from backend.conversation.projection import project_messages
from backend.conversation.resume import recover_session
from backend.conversation.store import SessionStore


def call(id):
    return {
        "id": id,
        "type": "function",
        "function": {"name": "Shell", "arguments": '{"command":"echo hi"}'},
    }


def test_projection_preserves_real_roles_and_fills_only_missing_results():
    events = [
        {"type": "message", "message_id": "s", "role": "system", "content": "policy"},
        {"type": "message", "message_id": "u", "role": "user", "content": "task"},
        {
            "type": "message",
            "message_id": "a",
            "role": "assistant",
            "content": "working",
            "tool_calls": [call("one"), call("two")],
        },
        {"type": "tool_result", "call_id": "one", "content": "success"},
        {"type": "tool_result", "call_id": "one", "content": "duplicate"},
        {"type": "message", "message_id": "next", "role": "user", "content": "continue"},
    ]
    projected = project_messages(events)
    assert [m["role"] for m in projected] == ["system", "user", "assistant", "tool", "tool", "user"]
    assert projected[3]["content"] == "success"
    assert projected[4]["tool_call_id"] == "two"
    assert json.loads(projected[4]["content"])["status"] == "unknown"


def test_partial_tool_arguments_are_never_projected_as_calls():
    result = project_messages(
        [
            {
                "type": "message",
                "message_id": "a",
                "role": "assistant",
                "status": "partial",
                "content": "part",
                "tool_calls": [call("one")],
            }
        ]
    )
    assert len(result) == 1
    assert "tool_calls" not in result[0]
    assert "part" in result[0]["content"]


def test_recovery_records_partial_reply_and_never_replays_operations(tmp_path):
    store = SessionStore(tmp_path)
    store.append("turn_start", turn_id="turn")
    store.append(
        "message",
        turn_id="turn",
        message_id="a",
        role="assistant",
        content="",
        tool_calls=[call("done"), call("unknown")],
    )
    store.append("tool_result", turn_id="turn", call_id="done", content="already written")
    store.append(
        "assistant_delta", turn_id="turn", message_id="partial", kind="text", content="hello "
    )
    store.append(
        "assistant_delta", turn_id="turn", message_id="partial", kind="text", content="world"
    )
    restored = recover_session(SessionStore(tmp_path, store.session_id))
    count = len(restored.events)
    recover_session(restored)
    assert len(restored.events) == count
    assert restored.meta["last_termination"] == "process_exit"
    messages = project_messages(restored.events)
    assert messages[-1]["content"].startswith("hello world")
    assert messages[1]["content"] == "already written"
    assert json.loads(messages[2]["content"])["status"] == "unknown"
    assert not any(e["type"] == "tool_start" for e in restored.events)


def test_manager_prevents_concurrent_rounds_and_duplicate_finish(tmp_path):
    store = SessionStore(tmp_path)
    manager = ConversationManager(store)
    manager.start("question")
    with pytest.raises(RuntimeError):
        manager.start("overlap")
    manager.message("assistant", "Need a path", is_final=True)
    manager.finish("completed", "stop")
    manager.finish("completed", "stop")
    assert len([e for e in store.events if e["type"] == "turn_end"]) == 1
    manager.start("new question")
    assert manager.active


def test_deferred_completion_stays_out_of_history_after_checkpoint_and_recovery(tmp_path):

    store = SessionStore(tmp_path)
    store.append("turn_start", turn_id="old")
    store.append("completion_check", turn_id="old", message_id="candidate", status="blocked")
    store.append("message", turn_id="old", message_id="candidate", role="assistant", content="完成")
    store.append("turn_end", turn_id="old", status="paused")
    store.append("turn_start", turn_id="new")
    store.append("assistant_delta", turn_id="new", message_id="deferred", kind="text", content="完成")
    store.append("completion_deferred", turn_id="new", message_id="deferred", content="完成")
    store.save_snapshot()
    restored = recover_session(SessionStore(tmp_path, store.session_id))
    assert project_messages(restored.events) == []
    assert project_messages(restored.view) == []
