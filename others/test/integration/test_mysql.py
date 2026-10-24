"""Real MySQL contracts. Set MINICODE_TEST_MYSQL_SOCKET for an isolated test server."""
import os

import pytest

from backend.config import Config


@pytest.fixture
def database(tmp_path):
    socket = os.environ.get("MINICODE_TEST_MYSQL_SOCKET")
    if not socket:
        pytest.skip("isolated MySQL not configured")
    import uuid

    import pymysql

    from backend.storage.mysql import MySQLDatabase

    name = "minicode_test_" + uuid.uuid4().hex
    options = dict(unix_socket=socket, user="root", autocommit=True)
    connection = pymysql.connect(**options)
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{name}`")
    app_root = tmp_path / "app"
    app_root.mkdir()
    config = Config(app_root, "test-secret", storage_mode="mysql", mysql_host="localhost",
                    mysql_database=name, mysql_user="root", context_window=100000,
                    max_output_tokens=1000)
    db = MySQLDatabase(config, connect=lambda: pymysql.connect(database=name, **options,
        cursorclass=pymysql.cursors.DictCursor))
    yield db
    db.close()
    with connection.cursor() as cursor:
        cursor.execute(f"DROP DATABASE `{name}`")
    connection.close()


def test_mysql_context_and_checkpoint_reopen_from_suffix(database, tmp_path):
    from backend.conversation.projection import project_messages
    from backend.storage.mysql_store import MySQLSessionStore

    store = MySQLSessionStore(database, tmp_path)
    store.append("message", message_id="u", role="user", content="保留我的任务")
    store.save_snapshot()
    store.append("message", message_id="a", role="assistant", content="已保存", is_final=True)
    reopened = MySQLSessionStore(database, tmp_path, store.session_id)
    assert reopened.recovery_info["source"] == "journal_snapshot"
    assert reopened.recovery_info["loaded_records"] == 1
    assert [m["content"] for m in project_messages(reopened.view)] == ["保留我的任务", "已保存"]
    assert len(reopened.events) == 2
    assert not (reopened.directory / "session.jsonl").exists()


def test_session_owner_and_stale_writer_cannot_interleave(database, tmp_path):
    from backend.storage.mysql_store import MySQLSessionStore

    first = MySQLSessionStore(database, tmp_path)
    second = MySQLSessionStore(database, tmp_path, first.session_id)
    first.acquire_owner()
    with pytest.raises(ValueError, match="占用"):
        second.acquire_owner()
    first.append("message", role="user", message_id="u", content="唯一输入")
    with pytest.raises(ValueError, match="写入权"):
        second.append("message", role="user", message_id="other", content="过期输入")
    assert len(first.events) == 1
    first.close()
    second.acquire_owner()
    assert second.seq == 1


async def test_workspace_overlap_unknown_and_persistent_reconciliation(database, tmp_path):
    from backend.conversation.resume import recover_session
    from backend.execution.mysql_gate import mysql_execution_gate
    from backend.storage.mysql_store import MySQLSessionStore

    a = MySQLSessionStore(database, tmp_path)
    b = MySQLSessionStore(database, tmp_path)
    a.acquire_owner()
    b.acquire_owner()
    async with mysql_execution_gate(a, [tmp_path / "project"]):
        a.append("tool_start", call_id="write", name="WriteFile", input={"content": "after"})
        a.append("operation_prepared", call_id="write", name="WriteFile")
        a.append("tool_running", call_id="write", name="WriteFile")
    from backend.tools.base import ToolError
    with pytest.raises(ToolError, match="核实"):
        async with mysql_execution_gate(b, [tmp_path / "project" / "nested"]):
            pytest.fail("overlapping unknown must remain blocked")
    async with mysql_execution_gate(b, [tmp_path / "other"]):
        pass
    recover_session(a)
    unknown = next(e for e in a.view if e["type"] == "tool_result")
    assert unknown["status"] == "unknown"
    async with mysql_execution_gate(a, [], reconciliation=True) as lease:
        a.append("operation_reconciled", call_id="write", result_seq=unknown["seq"],
                 resolution="succeeded", evidence="核查文件为 after，旧进程已停止", processes_stopped=True)
        lease.clear_for(a, unknown)
    async with mysql_execution_gate(b, [tmp_path / "project"]):
        pass
    a.close()
    reopened = MySQLSessionStore(database, tmp_path, a.session_id)
    from backend.conversation.operations import unresolved_operations
    assert not unresolved_operations(reopened.view)


async def test_real_runtime_uses_mysql_without_local_journal(database, tmp_path):
    import httpx

    from backend.api.app import create_app
    from test.integration.test_web import ScriptedProvider, until

    app = create_app(database.config, ScriptedProvider(), initialize_sandbox=False, database=database)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765",
            headers={"X-Minicode-Token": app.state.token}) as client:
            info = (await client.get("/api/info")).json()
            assert info["storage"]["active_mode"] == "mysql"
            created = (await client.post("/api/sessions")).json()
            sid = created["session"]["session_id"]
            result = await client.post(f"/api/sessions/{sid}/runs", json={"text": "持久保存此任务"})
            assert result.status_code == 202
            await until(lambda: not app.state.service.get(sid).busy())
            detail = (await client.get(f"/api/sessions/{sid}")).json()
            assert detail["messages"][0]["content"] == "持久保存此任务"
            assert not (database.config.root / ".minicode" / "sessions" / sid / "session.jsonl").exists()


def test_committed_receipt_is_reused_without_repeating_action(database):
    calls = []
    def action(cursor):
        calls.append("once")
        return {"saved": "durable"}
    stable_id = "1" * 32
    assert database.transaction(action, command_id=stable_id) == {"saved": "durable"}
    assert database.transaction(action, command_id=stable_id) == {"saved": "durable"}
    assert calls == ["once"]


@pytest.fixture
def fault_database(database):
    import pymysql

    from backend.storage.mysql import MySQLDatabase

    faults = {"commit": None, "query_down": False}
    class Connection:
        def __init__(self, actual):
            self.actual = actual
        def __getattr__(self, name):
            return getattr(self.actual, name)
        def commit(self):
            mode = faults["commit"]
            faults["commit"] = None
            if mode == "before":
                self.actual.rollback()
            else:
                self.actual.commit()
            if mode:
                faults["query_down"] = mode == "after_unreachable"
                raise pymysql.OperationalError(2013, "lost commit acknowledgement")
    def connect():
        if faults["query_down"]:
            raise pymysql.OperationalError(2003, "unreachable during verification")
        return Connection(pymysql.connect(unix_socket=os.environ["MINICODE_TEST_MYSQL_SOCKET"],
            user="root", database=database.config.mysql_database, autocommit=True,
            cursorclass=pymysql.cursors.DictCursor))
    db = MySQLDatabase(database.config, connect=connect)
    yield db, faults
    db.close()


def test_lost_commit_acknowledgement_queries_stable_id(fault_database, tmp_path):
    from backend.storage.mysql_store import MySQLSessionStore
    db, faults = fault_database
    store = MySQLSessionStore(db, tmp_path)
    faults["commit"] = "after"
    saved = store.append("message", role="user", message_id="u", content="只接单一次")
    assert store.find_event(saved["event_id"]) == saved
    assert len(store.events) == 1
    assert not store.failed


@pytest.mark.parametrize("mode,expected", [("before", 0), ("after_unreachable", 1)])
def test_uncertain_commit_freezes_writer_until_reopened(fault_database, tmp_path, mode, expected):
    from backend.storage.mysql_store import MySQLSessionStore
    db, faults = fault_database
    store = MySQLSessionStore(db, tmp_path)
    faults["commit"] = mode
    with pytest.raises(OSError, match="记录 ID"):
        store.append("message", role="user", message_id="u", content="核查接单结果")
    with pytest.raises(OSError, match="frozen"):
        store.append("message", role="user", message_id="bad", content="不得继续")
    faults["query_down"] = False
    reopened = MySQLSessionStore(db, tmp_path, store.session_id)
    assert reopened.seq == expected
    assert len(reopened.events) == expected


def test_independent_sessions_commit_concurrently_in_order(database, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from backend.storage.mysql_store import MySQLSessionStore
    stores = [MySQLSessionStore(database, tmp_path) for _ in range(12)]
    def write(store):
        for index in range(20):
            store.append("message", role="user", message_id=str(index), content=str(index))
        return store.events
    with ThreadPoolExecutor(max_workers=12) as workers:
        histories = list(workers.map(write, stores))
    assert all([e["seq"] for e in events] == list(range(1, 21)) for events in histories)
    assert all([e["content"] for e in events] == [str(i) for i in range(20)] for events in histories)


def test_abandoned_owner_requires_evidence_and_prevents_old_writer(database, tmp_path):
    from backend.runtime.service import RuntimeService
    from backend.storage.mysql_store import MySQLSessionStore
    from test.integration.test_web import ScriptedProvider

    old = MySQLSessionStore(database, tmp_path)
    old.acquire_owner()
    old.append("tool_start", call_id="write", name="WriteFile", input={"path": "x"})
    service = RuntimeService(database.config, ScriptedProvider(), database=database)
    with pytest.raises(ValueError, match="证据"):
        service.recover_owner(old.session_id, old.owner, "", True)
    service.recover_owner(old.session_id, old.owner, "核查进程树全部停止", True)
    restored = service.get(old.session_id)
    assert restored.store.owner != old.owner
    with pytest.raises(ValueError, match="写入权"):
        old.append("tool_result", call_id="write", status="succeeded")
    assert any(e["type"] == "owner_reconciled" for e in restored.store.events)
    restored.store.close()


async def test_mysql_runtime_file_tool_intent_result_and_reopen(database, tmp_path):
    import httpx

    from backend.api.app import create_app
    from backend.runtime.provider import Response
    from backend.storage.mysql_store import MySQLSessionStore
    from test.integration.test_web import ScriptedProvider, until
    from test.support.tooling import call

    project = tmp_path / "project"
    project.mkdir()
    target = project / "created.txt"
    provider = ScriptedProvider()
    provider.responses = [Response("", [call("WriteFile", {"path": str(target), "content": "after"}, "write")], "tool_calls", {})]
    app = create_app(database.config, provider, initialize_sandbox=False, database=database)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765",
            headers={"X-Minicode-Token": app.state.token}) as client:
            sid = (await client.post("/api/sessions")).json()["session"]["session_id"]
            await client.post(f"/api/sessions/{sid}/directories", json={"action": "add", "path": str(project)})
            result = await client.post(f"/api/sessions/{sid}/runs", json={"text": "创建文件", "agent_mode": "auto"})
            assert result.status_code == 202
            await until(lambda: not app.state.service.get(sid).busy())
            history = app.state.service.get(sid).store.events
            assert target.exists(), [(e["type"], e.get("content"), e.get("text")) for e in history]
            assert target.read_text() == "after"
            prepared = next(e for e in history if e["type"] == "operation_prepared")
            started = next(e for e in history if e["type"] == "tool_running")
            outcome = next(e for e in history if e["type"] == "tool_result")
            assert prepared["input"]["content"] == "after"
            assert prepared["seq"] < started["seq"] < outcome["seq"]
            assert outcome["status"] == "succeeded"
            assert prepared["operation_id"] == started["operation_id"] == outcome["operation_id"]
            store = MySQLSessionStore(database, tmp_path, sid)
            assert store.recovery_info["source"] == "journal_snapshot"


async def test_row_lock_wait_does_not_block_other_session_cancel(database):
    import asyncio
    import threading

    import httpx
    import pymysql

    from backend.api.app import create_app
    from test.integration.test_web import ScriptedProvider, until

    provider = ScriptedProvider()
    provider.block = asyncio.Event()
    app = create_app(database.config, provider, initialize_sandbox=False, database=database)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765",
            headers={"X-Minicode-Token": app.state.token}) as client:
            first = (await client.post("/api/sessions")).json()["session"]["session_id"]
            second = (await client.post("/api/sessions")).json()["session"]["session_id"]
            blocker = pymysql.connect(unix_socket=os.environ["MINICODE_TEST_MYSQL_SOCKET"],
                user="root", database=database.config.mysql_database)
            with blocker.cursor() as cursor:
                cursor.execute("SELECT id FROM mc_sessions WHERE id=%s FOR UPDATE", (first,))
            released = threading.Event()
            def release():
                blocker.commit()
                blocker.close()
                released.set()
            timer = threading.Timer(1.5, release)
            timer.start()
            try:
                slow = asyncio.create_task(client.post(f"/api/sessions/{first}/runs", json={"text": "等待锁"}))
                await asyncio.sleep(0.03)
                async with asyncio.timeout(0.8):
                    accepted = await client.post(f"/api/sessions/{second}/runs", json={"text": "独立会话"})
                    assert accepted.status_code == 202
                    await until(lambda: provider.calls == 1)
                    cancelled = await client.post(f"/api/sessions/{second}/cancel")
                    assert cancelled.status_code == 200 and not cancelled.json()["running"]
                assert not released.is_set()
                assert (await slow).status_code == 202
                await client.post(f"/api/sessions/{first}/cancel")
            finally:
                timer.join()


async def test_mysql_and_local_batches_share_execution_boundary(database, tmp_path):
    import asyncio

    from backend.execution.gate import execution_gate
    from backend.execution.mysql_gate import mysql_execution_gate
    from backend.storage.mysql_store import MySQLSessionStore
    from backend.tools.base import ToolError

    store = MySQLSessionStore(database, tmp_path)
    store.acquire_owner()
    entered = asyncio.Event()
    async def mysql_batch():
        async with mysql_execution_gate(store, [tmp_path / "project"]):
            entered.set()
    async with execution_gate():
        pending = asyncio.create_task(mysql_batch())
        await asyncio.sleep(0.04)
        assert not entered.is_set()
    await pending
    # Even after the OS lock is released, another storage mode cannot bypass
    # a MySQL domain until that database has been verified and closed cleanly.
    with pytest.raises(ToolError, match="MySQL"):
        async with execution_gate():
            pytest.fail("local execution bypassed MySQL domain")


async def test_cancellation_after_workspace_claim_commit_releases_claim(database, tmp_path):
    import asyncio
    import threading

    import greenback
    import pymysql

    from backend.execution.mysql_gate import mysql_execution_gate
    from backend.storage.mysql_store import MySQLSessionStore

    store = MySQLSessionStore(database, tmp_path)
    store.acquire_owner()
    blocker = pymysql.connect(unix_socket=os.environ["MINICODE_TEST_MYSQL_SOCKET"],
        user="root", database=database.config.mysql_database)
    with blocker.cursor() as cursor:
        cursor.execute("SELECT id FROM mc_mutex WHERE id=1 FOR UPDATE")
    async def work():
        await greenback.ensure_portal()
        async with mysql_execution_gate(store, [tmp_path / "project"]):
            pytest.fail("cancelled before dispatch")
    task = asyncio.create_task(work())
    await asyncio.sleep(0.04)
    task.cancel()
    timer = threading.Timer(0.1, lambda: (blocker.commit(), blocker.close()))
    timer.start()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        async with mysql_execution_gate(store, [tmp_path / "project"]):
            pass
    finally:
        timer.join()


def test_frozen_runtime_writer_can_be_reopened_after_persistent_owner_review(fault_database):
    from backend.runtime.service import RuntimeService
    from test.integration.test_web import ScriptedProvider

    database, faults = fault_database
    service = RuntimeService(database.config, ScriptedProvider(), database=database)
    item = service.create()
    previous_owner = item.store.owner
    faults["commit"] = "before"
    with pytest.raises(OSError):
        item.store.append("notice", text="写入失败")
    listed = next(s for s in service.list_sessions() if s["session_id"] == item.store.session_id)
    assert listed["owner_needs_review"]
    service.recover_owner(item.store.session_id, previous_owner, "已核对旧任务及全部子进程停止", True)
    reopened = service.get(item.store.session_id)
    assert not reopened.store.failed
    assert reopened.store.owner != previous_owner
    reopened.store.close()


async def test_snapshot_links_survive_mysql_checkpoint_and_restore(database):
    from backend.api.app import create_app
    from backend.storage.mysql_store import MySQLSessionStore
    from test.integration.test_web import ScriptedProvider

    project = database.config.root.parent / "snapshot-project"
    project.mkdir()
    target = project / "note.txt"
    target.write_text("before\n")
    provider = ScriptedProvider()
    app = create_app(database.config, provider, initialize_sandbox=False, database=database)
    service = app.state.service
    try:
        item = service.create()
        item.permissions.add(str(project))
        item.start("snapshot round", agent_mode="auto")
        await item.task
        first = item.file_snapshots.listing()[0]
        repository, _ = item.file_snapshots.resolve(first["project_id"], first["id"])
        assert repository.files(first["before"]) == repository.files(first["after"])
        item.store.save_snapshot()
        reopened = MySQLSessionStore(database, database.config.root, item.store.session_id)
        completed = next(e for e in reopened.events if e["type"] == "file_snapshot_completed")
        assert completed["snapshot"]["project_id"] == first["project_id"]
        assert completed["snapshot"]["run_id"] == first["run_id"]
        assert any(e["type"] == "file_snapshot_completed" for e in reopened.view)
        assert not (item.store.directory / "session.jsonl").exists()
        target.write_text("manual\n")
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8765", headers={"X-Minicode-Token": app.state.token}) as client:
            url = f"/api/sessions/{item.store.session_id}/file-snapshots/{first['project_id']}/{first['id']}"
            plan = (await client.post(url + "/preview")).json()
            response = await client.post(url + "/restore", json={"token": plan["token"],
                                                                     "accept_conflicts": True})
            assert response.status_code == 200, response.text
        assert target.read_text() == "before\n"
        assert item.store.claim is None
    finally:
        # The fixture owns the test database; release service resources without
        # closing its shared connection pool until the fixture performs cleanup.
        await service.long_memory.close()
        for item in service.sessions.values():
            item.store.close()


async def test_mysql_text_readback_completes_once_and_coalesces_stream(database, tmp_path):
    import asyncio

    import httpx

    from backend.api.app import create_app
    from backend.runtime.provider import Response
    from test.support.tooling import call

    project = tmp_path / "project"
    project.mkdir()

    class Provider:
        requests = 0

        async def complete(self, messages, tools, on_delta, on_attempt, purpose="main"):
            await on_attempt(1)
            if purpose != "main":
                return Response("[]", [], "stop", None)
            self.requests += 1
            if self.requests == 1:
                return Response("", [call("WriteFile", {"path": "note.txt", "content": "哈基米南北绿豆\n"}, "write")], "tool_calls", None)
            if self.requests == 2:
                return Response("", [call("ReadFile", {"path": "note.txt"}, "read")], "tool_calls", None)
            for _ in range(1000):
                await on_delta({"type": "text", "content": "字"})
            return Response("字" * 1000, [], "stop", None)

    provider = Provider()
    app = create_app(database.config, provider, initialize_sandbox=False, database=database)
    app.state.service.sandbox._verified = True
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765",
            headers={"X-Minicode-Token": app.state.token}) as client:
            sid = (await client.post("/api/sessions")).json()["session"]["session_id"]
            assert (await client.post(f"/api/sessions/{sid}/directories", json={"action": "add", "path": str(project)})).status_code == 200
            assert (await client.post(f"/api/sessions/{sid}/runs", json={"text": "写入TXT并读回验证", "agent_mode": "auto"})).status_code == 202
            item = app.state.service.get(sid)
            async with asyncio.timeout(15):
                while item.busy():
                    await asyncio.sleep(0.01)
            detail = (await client.get(f"/api/sessions/{sid}")).json()
            assert item.loop.status == "completed"
            assert any(e["type"] == "file_verification_result" and e["status"] == "passed" for e in detail["events"])
            assert provider.requests == 3
            assert (project / "note.txt").read_text() == "哈基米南北绿豆\n"
            replies = [m for m in detail["messages"] if m["role"] == "assistant" and not m.get("tool_calls")]
            assert [m["content"] for m in replies] == ["字" * 1000]
            deltas = [e for e in detail["events"] if e["type"] == "assistant_delta"]
            assert "".join(e["content"] for e in deltas) == "字" * 1000
            assert len(deltas) < 100


@pytest.mark.skipif(os.name == "nt", reason="POSIX restart cleanup")
def test_launcher_shutdown_releases_mysql_session_for_the_next_backend(database):
    import subprocess
    import sys
    from pathlib import Path

    from backend.runtime.process import stop
    from backend.storage.mysql_store import MySQLSessionStore

    code = """
import asyncio, sys
from pathlib import Path
import pymysql
from backend.config import Config
from backend.execution import gate
from backend.runtime.service import RuntimeService
from backend.storage.mysql import MySQLDatabase
from backend.storage.project_lock import ProjectLock
root = Path(sys.argv[1])
gate.lock_directory = lambda: root.parent / 'child-gate'
gate.mysql_domain_path = lambda: gate.lock_directory() / 'mysql-domain.json'
config = Config(root, 'test-key', context_window=10000, max_output_tokens=1000,
                storage_mode='mysql', mysql_host='localhost', mysql_user='root',
                mysql_database=sys.argv[2])
async def main():
    db = MySQLDatabase(config, connect=lambda: pymysql.connect(unix_socket=sys.argv[3],
         user='root', database=config.mysql_database, autocommit=True,
         cursorclass=pymysql.cursors.DictCursor))
    service = RuntimeService(config, provider=object(), database=db)
    try:
        item = service.create()
        print(item.store.session_id, flush=True)
        await asyncio.Event().wait()
    finally:
        await service.close()
with ProjectLock(root):
    asyncio.run(main())
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(database.config.root), database.config.mysql_database,
         os.environ["MINICODE_TEST_MYSQL_SOCKET"]],
        cwd=Path(__file__).resolve().parents[3], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        sid = process.stdout.readline().strip()
        assert len(sid) == 32, process.stderr.read() if process.poll() is not None else sid
        assert stop(database.config.root)
        process.wait(timeout=5)
        reopened = MySQLSessionStore(database, database.config.root, sid)
        reopened.acquire_owner()
        assert reopened.owner
        reopened.close()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


async def test_local_session_can_save_mysql_configuration(database, monkeypatch):
    from dataclasses import replace

    import httpx

    from backend.api import storage
    from backend.api.app import create_app
    from backend.storage.mysql import MySQLDatabase
    from test.integration.test_web import ScriptedProvider

    # Validate the target against the isolated server, not the host's live MySQL.
    monkeypatch.setattr(storage, "MySQLDatabase", lambda config: MySQLDatabase(
        config, connect=database._connect))
    app = create_app(replace(database.config, storage_mode="local"), ScriptedProvider(),
                     initialize_sandbox=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8765",
                headers={"X-Minicode-Token": app.state.token}) as client:
            assert (await client.post("/api/sessions")).status_code == 201
            response = await client.put("/api/storage", json={
                "storage_mode": "mysql", "mysql_host": database.config.mysql_host,
                "mysql_database": database.config.mysql_database, "mysql_user": "root",
            })
            assert response.status_code == 200
            assert response.json()["active_mode"] == "local"
            assert response.json()["configured"]["storage_mode"] == "mysql"
            assert response.json()["restart_required"]
