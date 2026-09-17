import pytest

from memory.permission_state import SessionPermissions
from memory.session_store import SessionStore
from test.support.tooling import Outputs, call
from tools.base import ToolError
from tools.executor import ToolExecutor


@pytest.mark.asyncio
async def test_session_directory_grants_survive_resume_and_are_session_local(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "note.txt").write_text("outside project", encoding="utf-8")
    store = SessionStore(project)
    permissions = SessionPermissions(project, store)
    with pytest.raises(ToolError):
        permissions.add(str(tmp_path.anchor))
    assert permissions.add(str(extra)) == extra
    assert extra in SessionPermissions(project, SessionStore(project, store.session_id)).roots()
    assert extra not in SessionPermissions(project, SessionStore(project)).roots()

    approvals = []

    async def approve(request):
        approvals.append(request)
        return True

    executor = ToolExecutor(project, Outputs(project), approve, session_permissions=permissions)
    assert str(extra) in executor.permission_context()
    read = await executor.execute_batch([call("ReadFile", {"path": str(extra / "note.txt")})])
    assert read[0].status == "succeeded"
    assert "outside project" in read[0].content
    executor.mark_prompted(permissions.roots())
    created = await executor.execute_batch(
        [call("WriteFile", {"path": str(extra / "new.txt"), "content": "new"})]
    )
    assert created[0].status == "succeeded" and len(approvals) == 1
    permissions.remove(str(extra))
    assert str(extra) not in executor.permission_context()
    denied = await executor.execute_batch([call("ReadFile", {"path": str(extra / "note.txt")})])
    assert denied[0].error_type == "path"


def test_reject_system_and_internal_directory(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(project)
    permissions = SessionPermissions(project, store)
    for value in (str(project.anchor), str(project / ".minicode"), "relative"):
        with pytest.raises(ToolError):
            permissions.add(value)


def test_missing_grant_is_visible_but_not_effective_after_resume(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    store = SessionStore(project)
    SessionPermissions(project, store).add(str(extra))
    (extra / "project_system_prompt.md").unlink()
    (extra / "project_skills").rmdir()
    extra.rmdir()
    restored = SessionPermissions(project, SessionStore(project, store.session_id))
    assert extra not in restored.roots()
    assert restored.listing()[0]["valid"] is False
    restored.remove(str(extra))
    assert len(restored.listing()) == 0


@pytest.mark.asyncio
async def test_project_is_not_a_work_directory_and_active_grant_survives_resume(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    first = tmp_path / "work-one"
    second = tmp_path / "work-two"
    first.mkdir()
    second.mkdir()
    (project / "private.txt").write_text("internal", encoding="utf-8")
    (first / "note.txt").write_text("first", encoding="utf-8")
    store = SessionStore(project)
    permissions = SessionPermissions(project, store)
    output = Outputs(project)
    executor = ToolExecutor(project, output, lambda _: True, session_permissions=permissions)
    assert permissions.roots() == ()
    assert (await executor.execute_batch([call("Glob", {"pattern": "*"})]))[0].error_type == "path"
    assert (await executor.execute_batch([call("ReadFile", {"path": str(project / "private.txt")})]))[0].error_type == "path"
    saved = output.save("log", "saved tool output")
    assert (await executor.execute_batch([call("ReadFile", {"path": saved["path"]})]))[0].status == "succeeded"
    with pytest.raises(ToolError):
        permissions.add(str(tmp_path))
    permissions.add(str(first))
    prompt = first / "project_system_prompt.md"
    assert prompt.is_file() and (first / "project_skills").is_dir()
    prompt.write_text("keep this", encoding="utf-8")
    permissions.select(str(first))
    assert prompt.read_text(encoding="utf-8") == "keep this"
    permissions.add(str(second))
    permissions.select(str(first))
    restored = SessionPermissions(project, SessionStore(project, store.session_id))
    assert restored.active() == first
    resumed = ToolExecutor(project, output, lambda _: True, session_permissions=restored)
    assert (await resumed.execute_batch([call("ReadFile", {"path": "note.txt"})]))[0].status == "succeeded"
    assert (await resumed.execute_batch([call("ReadFile", {"path": str(project / "private.txt")})]))[0].error_type == "path"
    assert (await resumed.execute_batch([call("ReadFile", {"path": str(prompt)})]))[0].status == "failed"
