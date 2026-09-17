"""Dispatch real terminal key sequences through prompt_toolkit's input pipe."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from app.terminal import TerminalApp
from config import Config
from conversation.provider import Response
from memory.session_store import SessionStore


class NoCallsProvider:
    calls = 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("Selecting history must not call model")


async def test_unknown_process_blocks_session_switch_and_submission(tmp_path):
    async with terminal(tmp_path) as (app, pipe, provider):
        original = app.store.session_id
        app.tool_event(
            "tool_result",
            call_id="unknown-shell",
            name="Shell",
            status="unknown",
            content="termination not confirmed",
            error_type="termination_unknown",
        )
        pipe.send_text("/new\r")
        await until(lambda: "当前禁止" in app.output_area.text)
        assert app.store.session_id == original
        assert not app.submit("/session")
        assert not app.submit("继续")
        assert provider.calls == 0


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


@asynccontextmanager
async def terminal(tmp_path):
    with create_pipe_input() as pipe:
        provider = NoCallsProvider()
        app = TerminalApp(
            Config(tmp_path, "test-key", context_window=10000, max_output_tokens=1000),
            provider=provider,
            input=pipe,
            output=DummyOutput(),
        )
        running = asyncio.create_task(app.run())
        try:
            await until(lambda: app.application.is_running)
            yield app, pipe, provider
        finally:
            if app.application.is_running:
                app.application.exit()
            await asyncio.wait_for(running, 3)


async def test_session_enter_lists_selects_without_model_call(tmp_path):
    old = SessionStore(tmp_path)
    old.append("message", role="user", content="old task", message_id="old-message")
    old.append("turn_end", status="interrupted", reason="user_cancelled")
    async with terminal(tmp_path) as (app, pipe, provider):
        pipe.send_text("/session\r")
        await until(lambda: bool(app.choices))
        assert "选择会话" in app.choice_text()
        index = next(
            i for i, item in enumerate(app.choices) if item["session_id"] == old.session_id
        )
        pipe.send_text("\x1b[B" * index + "\r")
        await until(lambda: app.store.session_id == old.session_id)
        assert not app.choices and not app.busy
        assert "old task" in app.output_area.text
        assert provider.calls == 0


async def test_terminal_enables_mouse_scrolling_and_clickable_transcript(tmp_path):
    async with terminal(tmp_path) as (app, pipe, provider):
        assert app.application.mouse_support()
        assert app.output_area.buffer.read_only()
        assert app.output_area.buffer is not app.input_area.buffer
        assert app.output_area.window is not None
        assert app.output_area.window.content is not None


async def test_busy_enter_preserves_draft_and_esc_cancels(tmp_path):
    async with terminal(tmp_path) as (app, pipe, provider):
        stopped = asyncio.Event()

        async def worker():
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        app.worker = asyncio.create_task(worker())
        pipe.send_text("keep this draft\r")
        await until(lambda: "草稿已保留" in app.output_area.text)
        assert app.input_area.text == "keep this draft" and app.busy
        pipe.send_text("\x1b")
        await asyncio.wait_for(stopped.wait(), 3)
        assert app.input_area.text == "keep this draft" and provider.calls == 0


@pytest.mark.parametrize("key, expected", [("\x19", True), ("\x0e", False), ("\x1b", "cancelled")])
async def test_approval_key_dispatch_preserves_input_focus(tmp_path, key, expected):
    async with terminal(tmp_path) as (app, pipe, provider):
        results = []

        async def worker():
            try:
                results.append(
                    await app.ask_approval({"description": "Shell in project; timeout 120"})
                )
            except asyncio.CancelledError:
                results.append("cancelled")

        app.worker = asyncio.create_task(worker())
        await until(lambda: app.approval is not None)
        pipe.send_text("draft\r")
        await until(lambda: "草稿已保留" in app.output_area.text)
        assert app.approval is not None and not app.approval.done()
        pipe.send_text(key)
        await until(lambda: bool(results))
        assert results == [expected]
        assert app.input_area.text == "draft" and provider.calls == 0
        assert app.application.layout.has_focus(app.input_area)


async def test_directory_commands_and_browsable_revoke(tmp_path):
    extra = tmp_path.parent / (tmp_path.name + " extra")
    extra.mkdir()
    async with terminal(tmp_path) as (app, pipe, provider):
        assert app.submit(f"/allow-dir {extra}")
        assert extra in app.directory_permissions.roots()
        assert app.submit("/dirs")
        assert "工作目录" in app.choice_text()
        pipe.send_text("d")
        await until(lambda: extra not in app.directory_permissions.roots())
        assert provider.calls == 0


async def test_approval_details_expand_without_losing_draft(tmp_path):
    async with terminal(tmp_path) as (app, pipe, provider):
        task = asyncio.create_task(
            app.ask_approval({"description": "change summary", "details": "full diff\n+new line"})
        )
        await until(lambda: app.approval is not None)
        app.input_area.text = "draft"
        pipe.send_text(" ")
        await until(lambda: app.approval_expanded)
        assert "full diff" in app.approval_area.text
        assert app.input_area.text == "draft"
        pipe.send_text("\x0e")
        assert await asyncio.wait_for(task, 3) is False
        assert provider.calls == 0


async def test_skill_selection_and_collapsed_tool_details(tmp_path):
    skill = tmp_path / "skill" / "skills" / "frontend"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("description: Frontend work\nUse accessible files only.", encoding="utf-8")
    second = tmp_path / "skill" / "skills" / "testing"
    second.mkdir()
    (second / "SKILL.md").write_text("description: Test behavior", encoding="utf-8")
    async with terminal(tmp_path) as (app, pipe, provider):
        assert app.submit("/skill")
        assert "frontend" in app.choice_text()
        assert "testing" in app.choice_text()
        assert "Frontend work" in app.choice_text()
        assert "Test behavior" in app.choice_text()
        pipe.send_text("\r")
        await until(lambda: app.pending_skill == "frontend")
        assert app.skill_context() == ""
        app.tool_event("tool_pending", call_id="one", name="Glob")
        app.tool_event("tool_result", call_id="one", name="Glob", status="succeeded", content="very long result")
        assert "very long result" not in app.output_area.text
        assert "succeeded" in app.tool_text()
        pipe.send_text("\x14")
        await until(lambda: app.tool_expanded)
        assert app.tool_details.text == "very long result"
        app.submit("/tools")
        assert "Glob" in app.choice_text()
        assert provider.calls == 0


async def test_selected_skill_restored_with_session(tmp_path):
    skill = tmp_path / "skill" / "skills" / "frontend"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("description: UI\nUse files only after authorization.", encoding="utf-8")
    async with terminal(tmp_path) as (app, pipe, provider):
        session = app.store.session_id
        app.submit("/skill frontend")
        app.select_session(session)
        assert app.pending_skill == "frontend"
        app.forced_skill_for_turn = (app.pending_skill, "global", None)
        assert "after authorization" in app.skill_context()


async def test_forced_skill_only_applies_to_next_user_turn(tmp_path):
    skill = tmp_path / "skill" / "skills" / "frontend"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("description: UI\nSPECIAL_LAYOUT_RULE", encoding="utf-8")

    class CaptureProvider:
        def __init__(self):
            self.systems = []

        async def complete(self, messages, tools, on_delta, on_attempt):
            await on_attempt(1)
            self.systems.append(messages[0]["content"])
            return Response("done", [], "stop", None)

    async with terminal(tmp_path) as (app, pipe, _):
        app.submit("/skill frontend")
        session = app.store.session_id
        app.select_session(session)
        provider = CaptureProvider()
        app.agent.provider = provider
        assert app.submit("first task")
        await until(lambda: len(provider.systems) == 1 and not app.busy)
        assert "SPECIAL_LAYOUT_RULE" in provider.systems[0]
        assert app.pending_skill is None
        assert app.submit("second task")
        await until(lambda: len(provider.systems) == 2 and not app.busy)
        assert "SPECIAL_LAYOUT_RULE" not in provider.systems[1]
        assert "frontend: UI" in provider.systems[1]
        assert [event["type"] for event in app.store.events if event["type"].startswith("skill_")] == [
            "skill_queued", "skill_consumed"
        ]


async def test_directory_selection_from_list_changes_active_work_directory(tmp_path):
    first = tmp_path.parent / (tmp_path.name + " first")
    second = tmp_path.parent / (tmp_path.name + " second")
    first.mkdir()
    second.mkdir()
    async with terminal(tmp_path) as (app, pipe, provider):
        app.submit(f"/allow-dir {first}")
        app.submit(f"/allow-dir {second}")
        assert app.directory_permissions.active() == second
        app.submit("/dirs")
        pipe.send_text("\r")
        await until(lambda: app.directory_permissions.active() == first)
        assert app.executor.policy.base() == first
        assert provider.calls == 0


async def test_project_assets_and_prompt_editor_keys(tmp_path):
    project = tmp_path.parent / (tmp_path.name + " work")
    project.mkdir()
    async with terminal(tmp_path) as (app, pipe, provider):
        app.submit(f"/allow-dir {project}")
        prompt = project / "project_system_prompt.md"
        assert prompt.is_file() and (project / "project_skills").is_dir()
        prompt.write_text("Existing project rule", encoding="utf-8")
        app.submit("/see system_prompt")
        assert len(app.choices) == 2
        pipe.send_text("\x1b[B\r")
        await until(lambda: app.document is not None)
        assert app.view_area.text == "Existing project rule"
        pipe.send_text("\x1bOP")
        await until(lambda: app.editing)
        app.edit_area.text = "New project rule"
        app.edit_area.buffer.cursor_position = len(app.edit_area.text)
        pipe.send_text("\x0a")
        await until(lambda: "\n" in app.edit_area.text)
        app.edit_area.buffer.insert_text("second line")
        pipe.send_text("\r")
        await until(lambda: not app.editing)
        assert prompt.read_text(encoding="utf-8") == "New project rule\nsecond line"
        pipe.send_text("\x1bOP")
        await until(lambda: app.editing)
        app.edit_area.text = "discard this"
        pipe.send_text("\x1bOQ")
        await until(lambda: not app.editing)
        assert prompt.read_text(encoding="utf-8") == "New project rule\nsecond line"
        pipe.send_text("\x1bOQ")
        await until(lambda: app.document is None)
        assert provider.calls == 0


async def test_use_project_and_global_skills_are_distinct(tmp_path):
    project = tmp_path.parent / (tmp_path.name + " work")
    project.mkdir()
    global_skill = tmp_path / "skill" / "skills" / "review"
    global_skill.mkdir(parents=True)
    (global_skill / "SKILL.md").write_text("description: Global review\nGLOBAL RULE", encoding="utf-8")
    async with terminal(tmp_path) as (app, pipe, provider):
        app.submit(f"/allow-dir {project}")
        local_skill = project / "project_skills" / "review"
        local_skill.mkdir()
        (local_skill / "SKILL.md").write_text("description: Project review\nPROJECT RULE", encoding="utf-8")
        app.submit("/use project skills")
        assert "Project review" in app.choice_text() and "Global review" not in app.choice_text()
        pipe.send_text("\x1bOP")
        await until(lambda: app.editing)
        app.edit_area.text = "description: Project review\nUPDATED PROJECT RULE"
        pipe.send_text("\r")
        await until(lambda: not app.editing)
        assert "UPDATED PROJECT RULE" in (local_skill / "SKILL.md").read_text(encoding="utf-8")
        pipe.send_text("\x1bOQ")
        await until(lambda: app.document is None)
        pipe.send_text("\r")
        await until(lambda: app.pending_skill == "review")
        assert app.pending_skill_scope == "project"
        assert app.pending_skill_project == str(project)
        app.submit("/use global skills review")
        assert app.pending_skill_scope == "global"
        app.submit("/use project skills review")
        assert app.pending_skill_scope == "project"
        assert app.pending_skill_project == str(project)
        assert provider.calls == 0
