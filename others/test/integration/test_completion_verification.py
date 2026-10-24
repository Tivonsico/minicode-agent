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


async def test_new_successful_edit_invalidates_verification_evidence(tmp_path):
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
    assert completion_check(session.store.view, None)["reason"] == "no_file_mutation"
    session.publish("tool_result", name="EditFile", status="succeeded", call_id="write")
    assert completion_check(session.store.view, None)["reason"] == "verification_stale"


async def test_text_write_readback_finishes_once_without_shell(tmp_path):
    from backend.storage.output_store import OutputStore
    from backend.tools.executor import ToolExecutor

    provider = ScriptedProvider([
        action("WriteFile", "write", path="note.txt", content="哈基米南北绿豆\n"),
        action("ReadFile", "read", path="note.txt"),
        final(), final(), final(),
    ])
    loop, store, _ = make_loop(tmp_path, provider)

    async def approve(_):
        return True

    executor = ToolExecutor(tmp_path, OutputStore(store), approve,
                            emit=lambda kind, **data: store.append(kind, **data))
    executor.shell_backend = SimpleNamespace(available=lambda: True)
    loop.executor = executor
    assert await loop.run("创建TXT，写入一行并读取确认") == "completed"
    assert (tmp_path / "note.txt").read_text() == "哈基米南北绿豆\n"
    replies = [e for e in store.events if e["type"] == "message"
               and e.get("role") == "assistant" and not e.get("tool_calls")]
    assert [e["content"] for e in replies] == ["完成"]
    assert loop.requests == 3


async def test_blocked_completion_is_not_replayed_as_another_user_turn(tmp_path):
    from backend.conversation.projection import project_messages

    provider = ScriptedProvider([
        action("EditFile", "write", path="solution.py"),
        final(), final(), final(),
    ])
    executor = EvidenceExecutor()
    loop, store, _ = make_loop(tmp_path, provider, executor)
    executor.store = store
    assert await loop.run("修复功能") == "paused"
    history = project_messages(store.events)
    replies = [m["content"] for m in history if m["role"] == "assistant"
               and not m.get("tool_calls")]
    assert len(replies) == 1
    assert "验证" in replies[0] and "完成" != replies[0]
    for messages in provider.messages[2:]:
        assert messages[-1]["role"] == "tool"
        assert "完成前核对未通过" in messages[0]["content"]


async def test_readback_cannot_replace_code_tests_or_cover_an_unread_file(tmp_path):
    from backend.storage.output_store import OutputStore
    from backend.tools.executor import ToolExecutor

    for folder, responses in [
        ("code", [action("WriteFile", "write", path="main.py", content="raise RuntimeError()"),
                  action("ReadFile", "read", path="main.py")]),
        ("partial", [action("WriteFile", "write", path="note.txt", content="first\nsecond\n"),
                     action("ReadFile", "read", path="note.txt", line_count=1)]),
        ("unread", [action("WriteFile", "first", path="first.txt", content="first"),
                    action("WriteFile", "second", path="second.txt", content="second"),
                    action("ReadFile", "read", path="second.txt")]),
    ]:
        root = tmp_path / folder
        root.mkdir()
        provider = ScriptedProvider(responses + [final(), final(), final()])
        loop, store, _ = make_loop(root, provider)

        async def approve(_):
            return True

        executor = ToolExecutor(root, OutputStore(store), approve,
                                emit=lambda kind, **data: store.append(kind, **data))
        executor.shell_backend = SimpleNamespace(available=lambda: True)
        loop.executor = executor
        assert await loop.run("按要求修改并验证") == "paused", folder
        checks = [e for e in store.events if e["type"] == "completion_check"]
        assert all(not e["accepted"] for e in checks), folder


async def test_passing_code_test_can_be_followed_by_verified_text_note(tmp_path):
    class MixedExecutor(EvidenceExecutor):
        async def execute_batch(self, calls):
            results = await super().execute_batch(calls)
            if calls[0]["function"]["name"] == "ReadFile":
                write = next(e for e in reversed(self.store.events)
                             if e["type"] == "tool_result" and e.get("call_id") == "note")
                self.store.append("file_verification_result", status="passed", source="readback",
                                  mutations=[{"call_id": "note", "request_id": write["request_id"]}])
            return results

    provider = ScriptedProvider([
        action("EditFile", "code", path="main.py"),
        action("Shell", "test", command="pass", purpose="verification"),
        action("WriteFile", "note", path="note.txt", content="tested"),
        action("ReadFile", "read", path="note.txt"),
        final(), final(), final(),
    ])
    executor = MixedExecutor()
    loop, store, _ = make_loop(tmp_path, provider, executor)
    executor.store = store
    assert await loop.run("修复代码、测试并写入说明TXT") == "completed"
    checks = [e for e in store.events if e["type"] == "completion_check"]
    assert all(e["accepted"] for e in checks)
    assert any(e["review_requested"] for e in checks)


async def test_build_script_with_txt_suffix_still_requires_tests(tmp_path):
    from backend.storage.output_store import OutputStore
    from backend.tools.executor import ToolExecutor

    provider = ScriptedProvider([
        action("WriteFile", "write", path="CMakeLists.txt", content="invalid_command()"),
        action("ReadFile", "read", path="CMakeLists.txt"),
        final(), final(), final(),
    ])
    loop, store, _ = make_loop(tmp_path, provider)

    async def approve(_):
        return True

    executor = ToolExecutor(tmp_path, OutputStore(store), approve,
                            emit=lambda kind, **data: store.append(kind, **data))
    executor.shell_backend = SimpleNamespace(available=lambda: True)
    loop.executor = executor
    assert await loop.run("修改构建脚本") == "paused"
    assert all(not e["accepted"] for e in store.events if e["type"] == "completion_check")


async def test_long_text_can_be_verified_in_multiple_read_ranges(tmp_path):
    from backend.storage.output_store import OutputStore
    from backend.tools.executor import ToolExecutor

    provider = ScriptedProvider([
        action("WriteFile", "write", path="note.txt", content="text\n" * 2001),
        action("ReadFile", "read-start", path="note.txt", line_count=2000),
        action("ReadFile", "read-end", path="note.txt", start_line=2001),
        final(), final(), final(),
    ])
    loop, store, _ = make_loop(tmp_path, provider)

    async def approve(_):
        return True

    executor = ToolExecutor(tmp_path, OutputStore(store), approve,
                            emit=lambda kind, **data: store.append(kind, **data))
    executor.shell_backend = SimpleNamespace(available=lambda: True)
    loop.executor = executor
    assert await loop.run("创建长TXT并分段读取确认") == "completed"
    assert loop.requests == 4
