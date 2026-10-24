import pytest

from backend.conversation.manager import ConversationManager
from backend.conversation.resume import recover_session
from backend.conversation.store import SessionStore


def test_checkpoint_preserves_session_creation_time(tmp_path):
    store = SessionStore(tmp_path)
    first = store.append("mode_selected", mode="manual")
    store.append("mode_selected", mode="plan")
    ConversationManager(store).start("retain metadata")
    store.save_snapshot()
    restored = SessionStore(tmp_path, store.session_id)
    assert restored.meta["created_at"] == first["time"]
    assert restored._events_cache is None


def test_checkpoint_skips_prefix_and_replays_new_turn_without_stale_state(tmp_path):
    store = SessionStore(tmp_path, "one")
    ConversationManager(store).start("old objective")
    store.save_snapshot()
    boundary = store.seq
    manager = ConversationManager(store)
    manager.start("new objective")
    restored = SessionStore(tmp_path, "one")
    assert restored.recovery_info["source"] == "journal_snapshot"
    assert restored.recovery_info["covered_seq"] == boundary
    assert restored.recovery_info["loaded_records"] == 2
    recover_session(restored)
    assert next(e for e in reversed(restored.view) if e["type"] == "turn_end")["status"] == "interrupted"
    assert not any(e["type"] in {"checkpoint", "task_state"} for e in restored.events)
    assert [m["content"] for m in ConversationManager(restored).messages()] == [
        "old objective",
        "new objective",
    ]


def test_invalid_snapshot_falls_back_to_journal_without_losing_context(tmp_path):
    store = SessionStore(tmp_path, "two")
    manager = ConversationManager(store)
    manager.start("keep original")
    store.save_snapshot()
    (store.directory / "journal_snapshot.json").write_text('{"broken":true}')
    restored = SessionStore(tmp_path, "two")
    assert restored.recovery_info["source"] == "journal"
    assert ConversationManager(restored).messages() == manager.messages()


@pytest.mark.parametrize("checkpoint", [False, True])
def test_reused_tool_call_id_does_not_hide_unknown_after_checkpoint(tmp_path, checkpoint):
    store = SessionStore(tmp_path)
    store.append("tool_start", turn_id="old", request_id="r1", call_id="same", name="Shell")
    store.append(
        "tool_result",
        turn_id="old",
        request_id="r1",
        call_id="same",
        status="succeeded",
        content="old result",
    )
    store.append("tool_end", turn_id="old", request_id="r1", call_id="same", status="succeeded")
    store.append("turn_start", turn_id="new")
    store.append("tool_start", turn_id="new", request_id="r2", call_id="same", name="Shell")
    store.append("tool_running", turn_id="new", request_id="r2", call_id="same", name="Shell")
    if checkpoint:
        store.save_snapshot()
    restored = SessionStore(tmp_path, store.session_id)
    recover_session(restored)
    result = [
        e for e in restored.events if e["type"] == "tool_result" and e.get("request_id") == "r2"
    ]
    assert len(result) == 1
    assert result[0]["status"] == "unknown"
    count = restored.seq
    recover_session(restored)
    assert restored.seq == count


async def test_file_tool_after_restart_keeps_prefix_out_of_audit_cache(tmp_path):
    from backend.storage.output_store import OutputStore
    from backend.tools.executor import ToolExecutor
    from test.support.tooling import call

    store = SessionStore(tmp_path)
    (tmp_path / "hello.txt").write_text("hello")
    store.append("mode_selected", agent_mode="manual")
    store.save_snapshot()
    reopened = SessionStore(tmp_path, store.session_id)

    async def approve(request):
        return True

    executor = ToolExecutor(tmp_path, OutputStore(reopened), approve)
    result = await executor.execute_batch([call("ReadFile", {"path": "hello.txt"}, "read")])
    assert result[0].status == "succeeded"
    assert reopened._events_cache is None  # Explicit audit reads must remain lazy.
