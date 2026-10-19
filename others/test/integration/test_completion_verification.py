"""Completion cannot borrow old checks or invent extra user messages."""

import json
from types import SimpleNamespace

from backend.runtime.provider import Response
from backend.runtime.session import RuntimeSession
from backend.runtime.verification import completion_check
from backend.tools.base import ToolResult
from test.integration.test_agent_loop import FakeExecutor, FakeMemory, ScriptedProvider, make_loop
from test.support.tooling import call


class EvidenceExecutor(FakeExecutor):
    shell_backend = SimpleNamespace(available=lambda: True)

    async def execute_batch(self, calls):
        results = []
        for item in calls:
            name = item["function"]["name"]
            args = json.loads(item["function"]["arguments"])
            passed = args.get("command") != "fail"
            if name == "Shell":
                self.store.append(
                    "verification_result",
                    call_id=item["id"],
                    status="passed" if passed else "failed",
                    source="execution",
                )
            results.append(
                ToolResult(item["id"], name, "succeeded" if passed else "failed", "evidence")
            )
        return results


def action(name, identifier, **args):
    return Response("", [call(name, args, identifier)], "tool_calls", None)


def final():
    return Response("完成", [], "stop", None)


async def test_missing_check_requests_verification_without_forging_user_input(tmp_path):
    provider = ScriptedProvider(
        [
            action("WriteFile", "write", path="solution.py", content="x=1"),
            final(),
            action("Shell", "verify", command="pass", purpose="verification"),
            final(),
            final(),
        ]
    )
    executor = EvidenceExecutor()
    loop, store, memory = make_loop(tmp_path, provider, executor)
    executor.store = store
    assert await loop.run("实现功能") == "completed"
    checks = [e for e in store.events if e["type"] == "completion_check"]
    assert [e["accepted"] for e in checks] == [False, True, True]
    assert [e["status"] for e in checks] == ["blocked", "review_requested", "accepted"]
    assert "契约复核" in provider.messages[4][0]["content"]
    assert checks[0]["reason"] == "verification_missing"
    assert "完成前核对未通过" in provider.messages[2][0]["content"]
    assert len([e for e in store.events if e["type"] == "message" and e["role"] == "user"]) == 1
    assert len(memory.scheduled) == 1
    assert all(e["run_id"] and e["turn_id"] and e["request_id"] for e in checks)


async def test_pass_before_last_edit_is_stale(tmp_path):
    provider = ScriptedProvider(
        [
            action("Shell", "old", command="pass", purpose="verification"),
            action("EditFile", "write", path="solution.py"),
            final(),
            action("Shell", "fresh", command="pass", purpose="verification"),
            final(),
            final(),
        ]
    )
    executor = EvidenceExecutor()
    loop, store, _ = make_loop(tmp_path, provider, executor)
    executor.store = store
    assert await loop.run("修复功能") == "completed"
    checks = [e for e in store.events if e["type"] == "completion_check"]
    assert checks[0]["reason"] == "verification_stale"
    assert checks[-1]["verification_seq"] > checks[-1]["last_mutation_seq"]


async def test_missing_evidence_has_bounded_rechecks_and_no_memory_extraction(tmp_path):
    provider = ScriptedProvider(
        [
            action("EditFile", "write", path="solution.py"),
            final(),
            final(),
            final(),
        ]
    )
    executor = EvidenceExecutor()
    loop, store, memory = make_loop(tmp_path, provider, executor)
    executor.store = store
    assert await loop.run("修复功能") == "paused"
    ending = next(e for e in reversed(store.events) if e["type"] == "turn_end")
    assert ending["reason"] == "verification_required"
    assert loop.requests == 4 and not memory.scheduled
    assert not any(e.get("is_final") for e in store.events if e["type"] == "message")


async def test_contract_review_can_revise_code_but_cannot_reuse_old_pass(tmp_path):
    provider = ScriptedProvider(
        [
            action("EditFile", "initial", path="solution.py"),
            action("Shell", "initial-check", command="pass", purpose="verification"),
            final(),
            action("EditFile", "review-fix", path="solution.py"),
            final(),
            action("Shell", "review-check", command="pass", purpose="verification"),
            final(),
        ]
    )
    executor = EvidenceExecutor()
    loop, store, _ = make_loop(tmp_path, provider, executor)
    executor.store = store
    assert await loop.run("修复功能并保留异常契约") == "completed"
    checks = [e for e in store.events if e["type"] == "completion_check"]
    assert [e["status"] for e in checks] == ["review_requested", "blocked", "accepted"]
    assert checks[1]["reason"] == "verification_stale"
    assert sum(e["review_requested"] for e in checks) == 1


def test_prior_turn_and_old_pass_cannot_override_current_failure():
    events = [
        {"type": "verification_result", "turn_id": "old", "seq": 1, "status": "passed"},
        {
            "type": "tool_result",
            "turn_id": "new",
            "seq": 2,
            "name": "EditFile",
            "status": "succeeded",
        },
    ]
    assert completion_check(events, "new")["reason"] == "verification_missing"
    events += [
        {"type": "verification_result", "turn_id": "new", "seq": 3, "status": "passed"},
        {"type": "verification_result", "turn_id": "new", "seq": 4, "status": "failed"},
    ]
    assert completion_check(events, "new")["reason"] == "verification_failed"
    assert not completion_check(events, "new")["accepted"]


def test_read_only_work_does_not_require_mutation_verification():
    events = [
        {
            "type": "tool_result",
            "turn_id": "new",
            "seq": 1,
            "name": "ReadFile",
            "status": "succeeded",
        }
    ]
    assert not completion_check(events, "new")["required"]


async def test_new_successful_edit_invalidates_checkpoint_verification(tmp_path):
    from backend.config import Config

    sandbox = SimpleNamespace(executable="/bin/false", available=lambda: False, reason="test")
    session = RuntimeSession(
        Config(tmp_path, "test-key", context_window=10000, max_output_tokens=1000),
        "verification",
        ScriptedProvider([]),
        FakeMemory(),
        sandbox,
    )
    session.publish("verification_result", status="passed", call_id="check", source="execution")
    session.publish("tool_result", name="EditFile", status="failed", call_id="rejected")
    assert session.checkpoints.snapshot()["verification_status"] == "passed"
    session.publish("tool_result", name="EditFile", status="succeeded", call_id="write")
    assert session.checkpoints.snapshot()["verification_status"] == "stale"
    checkpoint = session.checkpoints.capture()
    assert checkpoint["state"]["verification_status"] == "stale"
