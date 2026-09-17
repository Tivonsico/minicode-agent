"""Exercise persisted grants, file approval, and fail-closed Shell together."""

import pytest

from memory.output_store import OutputStore
from memory.permission_state import SessionPermissions
from memory.session_store import SessionStore
from test.support.tooling import call
from tools.executor import ToolExecutor


@pytest.mark.asyncio
async def test_two_sessions_file_approvals_revoke_and_restore(tmp_path):
    project = tmp_path / "project"
    first = tmp_path / "first"
    second = tmp_path / "second"
    for directory in (project, first, second):
        directory.mkdir()
    target = first / "note.txt"
    target.write_text("before", encoding="utf-8")
    obsolete = second / "obsolete.txt"
    obsolete.write_text("remove me", encoding="utf-8")
    (first / ".env").write_text("DB_PASSWORD=secret", encoding="utf-8")

    store = SessionStore(project)
    grants = SessionPermissions(project, store)
    grants.add(str(first))
    grants.add(str(second))
    approvals = []

    async def approve(request):
        approvals.append(request)
        return True

    executor = ToolExecutor(
        project,
        OutputStore(store),
        approve,
        lambda kind, **data: store.append(kind, **data),
        grants,
    )
    read = await executor.execute_batch([call("ReadFile", {"path": str(target)}, "read")])
    assert read[0].status == "succeeded"
    executor.mark_prompted(grants.roots())
    edit = await executor.execute_batch(
        [call("EditFile", {"path": str(target), "old_text": "before", "new_text": "after"}, "edit")]
    )
    assert edit[0].status == "succeeded"
    deleted = await executor.execute_batch(
        [call("DeleteFile", {"path": str(obsolete)}, "delete")]
    )
    assert deleted[0].status == "succeeded"
    assert target.read_text(encoding="utf-8") == "after" and not obsolete.exists()
    assert [request["name"] for request in approvals] == ["EditFile", "DeleteFile"]

    sensitive = await executor.execute_batch(
        [call("ReadFile", {"path": str(first / ".env")}, "secret")]
    )
    assert sensitive[0].error_type == "sensitive"
    blocked = await executor.execute_batch(
        [call("Shell", {"command": "DROP DATABASE important"}, "drop")]
    )
    assert blocked[0].error_type == "dangerous_command"
    unavailable = await executor.execute_batch(
        [call("Shell", {"command": "git status"}, "status")]
    )
    assert unavailable[0].error_type == "shell_unavailable"
    assert len(approvals) == 2

    grants.remove(str(first))
    restored = SessionPermissions(project, SessionStore(project, store.session_id))
    assert restored.roots() == (second,)
    assert first not in ToolExecutor(project, OutputStore(store), approve, session_permissions=restored).policy.roots()
    other = SessionPermissions(project, SessionStore(project))
    assert other.roots() == ()
    assert [event["type"] for event in store.events if event["type"].startswith("directory_")] == [
        "directory_authorized", "directory_authorized", "directory_revoked"
    ]
