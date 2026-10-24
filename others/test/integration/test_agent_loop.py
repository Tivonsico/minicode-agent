"""Integration contracts: recorded history, scheduling and cancellation boundaries."""

import asyncio
from copy import deepcopy

import pytest

from backend.config import Config
from backend.context.project_assets import ProjectAssets
from backend.conversation.store import SessionStore
from backend.permissions.directories import SessionPermissions
from backend.runtime.loop import AgentLoop
from backend.runtime.provider import ProviderError, Response
from backend.storage.output_store import OutputStore
from backend.tools.base import ToolResult
from backend.tools.executor import ToolExecutor
from test.support.tooling import call


class FakeMemory:
    def __init__(self):
        self.scheduled = []

    def snapshot(self, scope=None):
        return []

    def schedule(self, *args, **kwargs):
        self.scheduled.append(args)


class FakeContext:
    last_estimate = 20

    async def prepare(self, messages, tools):
        return messages


class FakeExecutor:
    terminal_failed = False
    pause_reason = "repeated_tool_failure"

    def __init__(self):
        self.calls = []

    def reset_turn(self):
        self.terminal_failed = False

    def schemas(self):
        return []

    def permission_context(self):
        return "Current authorized directories: test project."

    async def execute_batch(self, calls):
        self.calls.extend(calls)
        return [
            ToolResult(c["id"], c["function"]["name"], "succeeded", "read result") for c in calls
        ]


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    async def complete(self, messages, tools, on_delta, on_attempt):
        await on_attempt(1)
        self.messages.append(deepcopy(messages))
        response = next(self.responses)
        if response.content:
            await on_delta({"type": "text", "content": response.content})
        return response


def make_loop(tmp_path, provider, executor=None, context=None):
    store = SessionStore(tmp_path)
    memory = FakeMemory()
    loop = AgentLoop(
        Config(tmp_path, "test-key", context_window=10000, max_output_tokens=1000),
        store,
        provider,
        executor or FakeExecutor(),
        context or FakeContext(),
        memory,
    )
    return loop, store, memory


def tool_response(call_id="call1", content="working"):
    return Response(
        content,
        [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "ReadFile", "arguments": '{"path":"a.txt"}'},
            }
        ],
        "tool_calls",
        {"total_tokens": 10},
        reasoning_content="preserved reasoning",
    )


async def test_agent_can_auto_select_multiple_installed_skills(tmp_path):
    for name, description, body in (
        ("frontend", "Build UI", "Use layout guidance."),
        ("tests", "Write tests", "Verify behavior."),
    ):
        directory = tmp_path / "skill" / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(f"description: {description}\n{body}", encoding="utf-8")
    provider = ScriptedProvider(
        [
            Response("", [call("UseSkill", {"names": ["frontend", "tests"]})], "tool_calls", None),
            Response("done", [], "stop", None),
        ]
    )
    store = SessionStore(tmp_path)

    async def approve(_request):
        raise AssertionError("Reading skills must not request approval")

    executor = ToolExecutor(tmp_path, OutputStore(store), approve)
    loop = AgentLoop(
        Config(tmp_path, "test-key", context_window=10000, max_output_tokens=1000),
        store,
        provider,
        executor,
        FakeContext(),
        FakeMemory(),
    )
    assert await loop.run("Build a UI and its tests") == "completed"
    assert "frontend: Build UI" in provider.messages[0][0]["content"]
    assert "tests: Write tests" in provider.messages[0][0]["content"]
    assert "Use layout guidance." in str(provider.messages[1])
    assert "Verify behavior." in str(provider.messages[1])


async def test_project_prompts_are_path_scoped_and_not_duplicated(tmp_path):
    app_root = tmp_path / "app"
    first = tmp_path / "first"
    second = tmp_path / "second"
    for path in (app_root, first, second):
        path.mkdir()
    assets = ProjectAssets(app_root)
    assets.ensure_global()
    assets.global_path().write_text("GLOBAL_MARKER", encoding="utf-8")
    store = SessionStore(app_root)
    grants = SessionPermissions(app_root, store)
    grants.add(str(first))
    grants.add(str(second))
    assets.project_path(first).write_text("FIRST_MARKER", encoding="utf-8")
    assets.project_path(second).write_text("SECOND_MARKER", encoding="utf-8")
    for root, marker in ((first, "FIRST_SKILL"), (second, "SECOND_SKILL")):
        skill = root / "project_skills" / "local"
        skill.mkdir()
        (skill / "SKILL.md").write_text(f"description: {marker}", encoding="utf-8")
    (second / "readme.txt").write_text("hello", encoding="utf-8")
    provider = ScriptedProvider(
        [
            Response(
                "", [call("ReadFile", {"path": str(second / "readme.txt")})], "tool_calls", None
            ),
            Response("done", [], "stop", None),
        ]
    )

    async def approve(_request):
        return True

    executor = ToolExecutor(app_root, OutputStore(store), approve, session_permissions=grants)
    loop = AgentLoop(
        Config(app_root, "test-key", context_window=10000, max_output_tokens=1000),
        store,
        provider,
        executor,
        FakeContext(),
        FakeMemory(),
    )
    assert await loop.run(f"Check {first} and {second}") == "completed"
    for messages in provider.messages:
        system = messages[0]["content"]
        assert system.count("GLOBAL_MARKER") == 1
        assert system.count("FIRST_MARKER") == 1
        assert system.count("SECOND_MARKER") == 1
        assert system.count("FIRST_SKILL") == 1
        assert system.count("SECOND_SKILL") == 1
        assert str(first) in system and str(second) in system


async def test_new_project_prompt_loaded_before_cross_project_write(tmp_path):
    app_root = tmp_path / "app"
    first = tmp_path / "first"
    second = tmp_path / "second"
    for path in (app_root, first, second):
        path.mkdir()
    store = SessionStore(app_root)
    grants = SessionPermissions(app_root, store)
    grants.add(str(second))
    grants.add(str(first))
    ProjectAssets(app_root).project_path(second).write_text("SECOND_WRITE_RULE", encoding="utf-8")
    target = second / "new.txt"
    provider = ScriptedProvider(
        [
            Response(
                "",
                [call("WriteFile", {"path": str(target), "content": "made"}, "one")],
                "tool_calls",
                None,
            ),
            Response(
                "",
                [call("WriteFile", {"path": str(target), "content": "made"}, "two")],
                "tool_calls",
                None,
            ),
            Response("done", [], "stop", None),
        ]
    )

    async def approve(_request):
        return True

    executor = ToolExecutor(app_root, OutputStore(store), approve, session_permissions=grants)
    loop = AgentLoop(
        Config(app_root, "test-key", context_window=10000, max_output_tokens=1000),
        store,
        provider,
        executor,
        FakeContext(),
        FakeMemory(),
    )
    assert await loop.run("Create a file in the other authorized project") == "completed"
    assert "SECOND_WRITE_RULE" not in provider.messages[0][0]["content"]
    assert provider.messages[1][0]["content"].count("SECOND_WRITE_RULE") == 1
    assert target.read_text(encoding="utf-8") == "made"


async def test_model_receives_current_directory_grants_each_turn(tmp_path):
    extra = tmp_path.parent / (tmp_path.name + "-authorized")
    extra.mkdir()
    store = SessionStore(tmp_path)
    grants = SessionPermissions(tmp_path, store)
    provider = ScriptedProvider([Response("done", [], "stop", None)] * 3)

    async def approve(_request):
        raise AssertionError("No tool call should request approval")

    executor = ToolExecutor(tmp_path, OutputStore(store), approve, session_permissions=grants)
    loop = AgentLoop(
        Config(tmp_path, "test-key", context_window=10000, max_output_tokens=1000),
        store,
        provider,
        executor,
        FakeContext(),
        FakeMemory(),
    )
    await loop.run("first")
    grants.add(str(extra))
    await loop.run("second")
    grants.remove(str(extra))
    await loop.run("third")
    system_messages = [request[0]["content"] for request in provider.messages]
    assert str(extra) not in system_messages[0]
    assert str(extra) in system_messages[1]
    assert str(extra) not in system_messages[2]


async def test_text_with_tool_calls_runs_tools_then_final(tmp_path):
    provider = ScriptedProvider(
        [tool_response(), Response("done", [], "stop", {"total_tokens": 20})]
    )
    loop, store, memory = make_loop(tmp_path, provider)
    assert await loop.run("read file") == "completed"
    assert len(loop.executor.calls) == 1 and len(provider.messages) == 2
    second = provider.messages[1]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]
    assert second[2]["reasoning_content"] == "preserved reasoning"
    assert second[3]["tool_call_id"] == "call1"
    assistants = [e for e in store.events if e["type"] == "message" and e["role"] == "assistant"]
    assert [e["is_final"] for e in assistants] == [False, True]
    assert store.meta["token_total"] == 30 and len(memory.scheduled) == 1


async def test_long_turn_keeps_counting_attempts_past_thirty(tmp_path):
    class Retrying:
        attempts = 0

        async def complete(self, messages, tools, on_delta, on_attempt):
            for i in range(1, 4):
                await on_attempt(i)
                self.attempts += 1
            if self.attempts == 36:
                return Response("done", [], "stop", None)
            return tool_response(str(self.attempts))

    provider = Retrying()
    loop, store, memory = make_loop(tmp_path, provider)
    assert await loop.run("keep working") == "completed"
    assert provider.attempts == loop.requests == 36
    assert len([e for e in store.events if e["type"] == "request_start"]) == 36
    assert len(memory.scheduled) == 1


async def test_cancel_preserves_partial_and_never_dispatches_tools(tmp_path):
    emitted = asyncio.Event()

    class Blocking:
        async def complete(self, messages, tools, on_delta, on_attempt):
            await on_attempt(1)
            await on_delta({"type": "text", "content": "partial answer"})
            await on_delta(
                {"type": "tool_delta", "content": [{"index": 0, "function": {"arguments": "{"}}]}
            )
            emitted.set()
            await asyncio.Event().wait()

    loop, store, memory = make_loop(tmp_path, Blocking())
    task = asyncio.create_task(loop.run("work"))
    await asyncio.wait_for(emitted.wait(), 2)
    task.cancel()
    assert await task == "interrupted"
    restored = SessionStore(tmp_path, store.session_id)
    partial = [e for e in restored.events if e["type"] == "message" and e["role"] == "assistant"]
    assert len(partial) == 1 and partial[0]["content"] == "partial answer"
    assert partial[0]["status"] == "partial" and not partial[0]["is_final"]
    assert not loop.executor.calls and not memory.scheduled
    assert restored.meta["last_termination"] == "user_cancelled"


async def test_executor_terminal_failure_pauses_without_new_request(tmp_path):
    class FailedExecutor(FakeExecutor):
        async def execute_batch(self, calls):
            self.terminal_failed = True
            return [ToolResult(c["id"], "ReadFile", "failed", "missing", "missing") for c in calls]

    provider = ScriptedProvider([tool_response()])
    loop, store, memory = make_loop(tmp_path, provider, FailedExecutor())
    assert await loop.run("work") == "paused"
    assert len(provider.messages) == 1 and not memory.scheduled
    assert store.meta["last_termination"] == "repeated_tool_failure"


@pytest.mark.parametrize("finish", ["length", "content_filter", "unknown"])
async def test_non_normal_finish_never_runs_tools_or_extracts_memory(tmp_path, finish):
    response = tool_response()
    response.finish_reason = finish
    loop, _store, memory = make_loop(tmp_path, ScriptedProvider([response]))
    assert await loop.run("work") == "paused"
    assert not loop.executor.calls and not memory.scheduled


async def test_disconnected_partial_is_saved_and_paused(tmp_path):
    class Disconnected:
        async def complete(self, messages, tools, on_delta, on_attempt):
            await on_attempt(1)
            await on_delta({"type": "text", "content": "partial"})
            raise ProviderError("connection lost", partial_content="partial", received_output=True)

    loop, store, memory = make_loop(tmp_path, Disconnected())
    assert await loop.run("work") == "paused"
    assert store.events[-1]["status"] == "paused"
    assert not memory.scheduled and not loop.executor.calls


async def test_current_directory_state_overrides_old_missing_grant_reply(tmp_path):
    app_root, work = tmp_path / "app", tmp_path / "work"
    app_root.mkdir()
    work.mkdir()
    provider = ScriptedProvider([Response("已找到", [], "stop", None)])
    loop, store, _ = make_loop(app_root, provider)
    store.append("message", message_id="old", role="assistant", content="没有授权任何工作目录")
    grants = SessionPermissions(app_root, store)
    grants.add(str(work))

    async def approve(_):
        return True

    loop.executor = ToolExecutor(app_root, OutputStore(store), approve, session_permissions=grants)
    assert await loop.run("现在读取文件") == "completed"
    system = provider.messages[0][0]["content"]
    assert system.rfind("Current authorized work directories") > system.rfind("项目提示词")
    assert "优先于历史回复" in system
    assert str(work) in system


async def test_many_stream_chunks_keep_exact_text_without_per_token_commits(tmp_path):
    class TokenProvider:
        async def complete(self, messages, tools, on_delta, on_attempt):
            await on_attempt(1)
            for _ in range(1000):
                await on_delta({"type": "text", "content": "字"})
            return Response("字" * 1000, [], "stop", None)

    loop, store, _ = make_loop(tmp_path, TokenProvider())
    assert await loop.run("流式输出") == "completed"
    deltas = [e for e in store.events if e["type"] == "assistant_delta"]
    assert "".join(e["content"] for e in deltas) == "字" * 1000
    assert len(deltas) < 100
    end = next(e for e in store.events if e["type"] == "request_end")
    assert all(e["seq"] < end["seq"] for e in deltas)


async def test_reused_tool_id_in_a_later_request_keeps_its_own_result(tmp_path):
    class DifferentResults(FakeExecutor):
        async def execute_batch(self, calls):
            self.calls.extend(calls)
            return [
                ToolResult(c["id"], "ReadFile", "succeeded", f"result-{len(self.calls)}")
                for c in calls
            ]

    provider = ScriptedProvider(
        [tool_response("reused"), tool_response("reused"), Response("done", [], "stop", None)]
    )
    loop, store, _ = make_loop(tmp_path, provider, DifferentResults())
    assert await loop.run("read twice") == "completed"
    results = [m for m in provider.messages[-1] if m["role"] == "tool"]
    assert [m["content"] for m in results] == ["result-1", "result-2"]
    recorded = [e for e in store.events if e["type"] == "tool_result"]
    assert len({e["request_id"] for e in recorded}) == 2


async def test_runtime_session_uses_messages_without_duplicate_task_state(tmp_path):
    from types import SimpleNamespace

    from backend.runtime.session import RuntimeSession

    provider = ScriptedProvider([Response("done", [], "stop", None), Response("done", [], "stop", None)])
    session = RuntimeSession(
        Config(tmp_path, "test-key", context_window=100000, max_output_tokens=1000),
        "stateprojection", provider, FakeMemory(),
        SimpleNamespace(executable="/bin/false", available=lambda: False, reason="test"),
    )
    for task in ["修复登录", "继续检查过期行为"]:
        session.start(task)
        await session.task
        assert session.loop.status == "completed"
        assert any(m.get("content") == task for m in provider.messages[-1])
        assert all('"runtime_state"' not in m.get("content", "") for m in provider.messages[-1])
        assert not any(e["type"] in {"checkpoint", "task_state"} for e in session.store.events)
    assert session.loop.status == "completed"
