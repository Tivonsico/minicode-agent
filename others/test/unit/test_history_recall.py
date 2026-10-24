"""Recall uses the current session's original user messages, not its summary."""

import json

from backend.context.history_recall import recall_for_turn
from backend.conversation.store import SessionStore
from backend.observability.scope import scope
from backend.storage.output_store import OutputStore
from backend.tools.executor import ToolExecutor
from test.support.tooling import call


async def test_recall_finds_original_requirement_without_exposing_other_sessions(tmp_path):
    store = SessionStore(tmp_path)
    original = store.append("message", role="user", message_id="original", turn_id="earlier",
                            content="Econprior: never use uv run -m.")
    other = SessionStore(tmp_path)
    other.append("message", role="user", message_id="other", content="OTHER_SESSION_PRIVATE")

    async def approve(_):
        raise AssertionError("History recall is read-only")

    executor = ToolExecutor(tmp_path, OutputStore(store), approve, history_recall=True)
    executor.agent_mode = "plan"
    result = (await executor.execute_batch([call("RecallHistory", {"query": "Econprior"})]))[0]
    assert result.status == "succeeded"
    data = json.loads(result.content)
    assert data["matches"][0]["seq"] == original["seq"]
    assert data["matches"][0]["content"] == "Econprior: never use uv run -m."
    result = (await executor.execute_batch([call("RecallHistory", {"query": "OTHER_SESSION_PRIVATE"})]))[0]
    assert json.loads(result.content)["matches"] == []


async def test_recall_bounds_results_and_skips_current_question(tmp_path):
    store = SessionStore(tmp_path)
    original = store.append("message", role="user", message_id="large", turn_id="old",
                            content="x" * 3000 + "Econprior constraint" + "y" * 5000)
    store.append("message", role="user", message_id="second", turn_id="old2",
                 content="Econprior second original")
    store.append("message", role="user", message_id="question", turn_id="current",
                 content="Econprior current question")

    async def approve(_):
        raise AssertionError("Read-only")

    executor = ToolExecutor(tmp_path, OutputStore(store), approve, history_recall=True)
    with scope(store, turn_id="current"):
        result = (await executor.execute_batch([
            call("RecallHistory", {"query": "Econprior", "limit": 1})]))[0]
    data = json.loads(result.content)
    assert len(data["matches"]) == 1
    assert data["matches"][0]["truncated"]
    assert len(data["matches"][0]["content"]) <= 2500
    assert "Econprior constraint" in data["matches"][0]["content"]
    assert data["next_after_seq"] == original["seq"]
    with scope(store, turn_id="current"):
        result = (await executor.execute_batch([
            call("RecallHistory", {"query": "Econprior", "after_seq": original["seq"]})]))[0]
    assert [r["message_id"] for r in json.loads(result.content)["matches"]] == ["second"]


async def test_recall_query_is_literal_and_cannot_choose_another_session(tmp_path):
    store = SessionStore(tmp_path)
    store.append("message", role="user", message_id="source", content="aXb")

    async def approve(_):
        raise AssertionError("Read-only")

    executor = ToolExecutor(tmp_path, OutputStore(store), approve, history_recall=True)
    result = (await executor.execute_batch([call("RecallHistory", {"query": "a.b"})]))[0]
    assert json.loads(result.content)["matches"] == []
    result = (await executor.execute_batch([
        call("RecallHistory", {"query": "aXb", "session_id": "other"})]))[0]
    assert result.error_type == "schema"


def test_program_recall_uses_original_source_without_model_tool_selection(tmp_path):
    store = SessionStore(tmp_path)
    original = store.append("message", role="user", message_id="policy",
                            content="BillingTool must use batch size 37 and region eu-north-1.")
    question = "What was the BillingTool policy?"
    store.append("message", role="user", message_id="old_question", content=question)
    recalled = json.loads(recall_for_turn(store, question))
    assert recalled["source_seq"] == original["seq"]
    assert recalled["content"] == "BillingTool must use batch size 37 and region eu-north-1."
    assert recall_for_turn(store, "UnrelatedService configuration?") == ""


async def test_history_recall_is_disabled_by_default(tmp_path):
    store = SessionStore(tmp_path)

    async def approve(_):
        raise AssertionError("Read-only")

    executor = ToolExecutor(tmp_path, OutputStore(store), approve)
    executor.begin_turn("Recall an earlier detail")
    assert executor.recalled_history == ""
    assert "RecallHistory" not in {s["function"]["name"] for s in executor.schemas()}
