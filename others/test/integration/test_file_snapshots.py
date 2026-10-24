"""File history at the same HTTP/run boundary used by the browser."""

import asyncio

from backend.runtime.provider import Response
from test.integration.test_web import ScriptedProvider, client, until
from test.support.tooling import call


async def test_runtime_file_tools_history_restore_and_revoked_grant(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.txt"
    target.write_text("user dirty\n")
    provider = ScriptedProvider()
    provider.responses = [
        Response("", [call("ReadFile", {"path": str(target)}, "read")], "tool_calls", {}),
        Response("", [call("EditFile", {"path": str(target), "old_text": "user dirty",
                                      "new_text": "agent edit\nnew line"}, "edit")], "tool_calls", {}),
    ]
    async with client(tmp_path, provider) as (connection, service, _, app):
        item = service.create()
        item.permissions.add(str(project))
        sid = item.store.session_id
        base = f"/api/sessions/{sid}/file-snapshots"
        item.start("修改文件", agent_mode="auto")
        await until(lambda: not item.busy())
        records = (await connection.get(base)).json()["snapshots"]
        assert len(records) == 1 and records[0]["before"] and records[0]["after"]
        selected = base + f"/{records[0]['project_id']}/{records[0]['id']}"
        changes = (await connection.get(selected)).json()
        assert (changes["added"], changes["deleted"]) == (2, 1)
        assert target.read_text() == "agent edit\nnew line\n"
        target.write_text("manual\n")
        plan = (await connection.post(selected + "/preview")).json()
        assert plan["conflicts"] == ["note.txt"]
        assert (await connection.post(selected + "/restore", json={"token": plan["token"]})).status_code == 409
        result = await connection.post(selected + "/restore", json={"token": plan["token"], "accept_conflicts": True})
        assert result.status_code == 200, result.text
        assert target.read_text() == "user dirty\n"
        events = item.store.events
        assert {"file_snapshot_started", "file_snapshot_completed", "file_snapshot_restored"} <= {e["type"] for e in events}
        from backend.conversation.resume import recover_session
        sequence = item.store.seq
        recover_session(item.store)
        assert item.store.seq == sequence  # Successful restore has no orphan lifecycle.
        item.permissions.remove(str(project))
        assert (await connection.get(selected)).status_code == 409


async def test_cancel_saves_both_states_and_never_started_run(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.txt"
    target.write_text("initial\n")
    provider = ScriptedProvider()
    provider.block = asyncio.Event()
    async with client(tmp_path, provider) as (connection, service, _, app):
        item = service.create()
        item.permissions.add(str(project))
        item.start("slow")
        await until(lambda: provider.calls == 1)
        target.write_text("shell or manual effect\n")
        await item.cancel()
        records = item.file_snapshots.listing()
        assert records[0]["status"] == "interrupted"
        repository, _ = item.file_snapshots.resolve(records[0]["project_id"], records[0]["id"])
        assert repository.changes(records[0]["id"])["deleted"] == 1
        item.start("cancel before dispatch")
        await item.cancel()
        records = item.file_snapshots.listing()
        assert len(records) == 2 and all(r["after"] for r in records)
        assert provider.calls == 1


async def test_real_shell_changes_are_included_and_failure_is_snapshotted(tmp_path):
    import os

    import pytest

    if os.name == "nt":
        pytest.skip("POSIX shell fixture")
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.txt"
    target.write_text("before\n")
    provider = ScriptedProvider()
    provider.responses = [Response("", [call("Shell", {
        "command": "printf 'from shell\\n' > note.txt; printf 'new\\n' > created.txt; exit 7"
    }, "shell")], "tool_calls", {})]
    async with client(tmp_path, provider) as (connection, service, _, app):
        item = service.create()
        item.permissions.add(str(project))
        backend = item.executor.shell_backend
        if not await backend.verify():
            pytest.skip(backend.reason)
        item.start("execute shell", agent_mode="auto")
        await until(lambda: not item.busy())
        record = item.file_snapshots.listing()[0]
        repository, _ = item.file_snapshots.resolve(record["project_id"], record["id"])
        data = repository.changes(record["id"])
        assert (data["added"], data["deleted"]) == (2, 1)
        assert target.read_text() == "from shell\n"
        assert any(e["type"] == "tool_result" and e.get("name") == "Shell"
                   and e["status"] == "failed" for e in item.store.events)
