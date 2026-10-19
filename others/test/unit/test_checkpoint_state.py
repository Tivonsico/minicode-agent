from backend.checkpoint import Checkpoints
from backend.conversation.store import SessionStore


def test_task_projection_recovers_without_copying_messages(tmp_path):
    store = SessionStore(tmp_path)
    state = Checkpoints(store)
    state.begin("任务目标", "turn")
    state.update(request_count=2, context_estimate=100, pending_tool={"ticket": "old"})
    store.append("turn_end", turn_id="turn", status="interrupted", reason="process_exit")
    restored = Checkpoints(SessionStore(tmp_path, store.session_id))
    assert restored.snapshot()["status"] == "interrupted"
    assert restored.snapshot()["pending_tool"] is None
    assert restored.snapshot()["request_count"] == 2
    assert "messages" not in restored.snapshot()
