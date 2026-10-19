"""A missing barrier must be caught by observed overlap, not scheduler internals."""

import asyncio
import threading

import pytest

from backend.tools.base import ToolResult
from test.support.tooling import call


async def test_separate_sessions_cannot_overlap_tools_in_one_workspace(tmp_path, setup_tools):
    from backend.tools.executor import ToolExecutor

    one, output, _, _ = setup_tools
    entered, release = asyncio.Event(), asyncio.Event()
    second_entered = asyncio.Event()

    async def first_approval(request):
        entered.set()
        await release.wait()
        return True

    async def second_approval(request):
        second_entered.set()
        return True

    one.approve = first_approval
    two = ToolExecutor(tmp_path, output, second_approval)
    first = asyncio.create_task(
        one.execute_batch([call("WriteFile", {"path": "one", "content": "1"}, "a")])
    )
    await entered.wait()
    second = asyncio.create_task(
        two.execute_batch([call("WriteFile", {"path": "two", "content": "2"}, "b")])
    )
    try:
        await asyncio.sleep(0.04)
        assert not second_entered.is_set()
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert (tmp_path / "one").read_text() == "1"
    assert (tmp_path / "two").read_text() == "2"


async def test_error_after_file_effect_pauses_following_tools(tmp_path, setup_tools, monkeypatch):
    from backend.tools import file_operations

    executor, *_ = setup_tools

    def fail(path):
        raise OSError("directory sync failed after replacement")

    monkeypatch.setattr(file_operations, "sync_directory", fail)
    results = await executor.execute_batch(
        [
            call("WriteFile", {"path": "effect", "content": "written"}, "one"),
            call("WriteFile", {"path": "must-not-run", "content": "no"}, "two"),
        ]
    )
    assert (tmp_path / "effect").read_text() == "written"
    assert results[0].status == "unknown"
    assert results[1].status == "cancelled"
    assert not (tmp_path / "must-not-run").exists()


async def test_unknown_effect_blocks_other_session_until_durable_reconciliation(
    tmp_path, monkeypatch
):
    from backend.conversation.store import SessionStore
    from backend.execution.gate import execution_gate
    from backend.storage.output_store import OutputStore
    from backend.tools import file_operations
    from backend.tools.executor import ToolExecutor

    async def approve(request):
        return True

    store = SessionStore(tmp_path, "owner")
    one = ToolExecutor(tmp_path, OutputStore(store), approve, store.append)
    two = ToolExecutor(tmp_path, OutputStore(SessionStore(tmp_path, "other")), approve)
    original = file_operations.sync_directory

    def fail(path):
        raise OSError("after effect")

    monkeypatch.setattr(file_operations, "sync_directory", fail)
    result = await one.execute_batch(
        [call("WriteFile", {"path": "effect", "content": "yes"}, "one")]
    )
    assert result[0].status == "unknown"
    monkeypatch.setattr(file_operations, "sync_directory", original)
    blocked = await two.execute_batch(
        [call("WriteFile", {"path": "blocked", "content": "no"}, "two")]
    )
    assert blocked[0].status == "cancelled"
    assert "未确认" in blocked[0].content
    assert not (tmp_path / "blocked").exists()
    unknown = next(e for e in store.events if e["type"] == "tool_result")
    async with execution_gate(reconciliation=True) as lease:
        store.append(
            "operation_reconciled",
            result_seq=unknown["seq"],
            call_id="one",
            resolution="succeeded",
            evidence="checked file and processes",
            processes_stopped=True,
        )
        lease.clear_for(store, unknown)
    two.reset_turn()
    result = await two.execute_batch(
        [call("WriteFile", {"path": "allowed", "content": "ok"}, "three")]
    )
    assert result[0].status == "succeeded"


async def test_result_commit_before_marker_cleanup_does_not_block_forever(tmp_path):
    from backend.conversation.store import SessionStore
    from backend.execution.gate import execution_gate

    store = SessionStore(tmp_path)
    store.append("tool_running", call_id="one", name="WriteFile")
    async with execution_gate() as lease:
        lease.before_effect(store, "one")
        store.append("tool_result", call_id="one", status="succeeded", content="done")
        # Simulate a crash before marker removal.
    async with execution_gate() as lease:
        assert lease.pending() is None


@pytest.mark.asyncio
async def test_read_concurrency_write_barrier_and_order(tmp_path, setup_tools):
    executor, *_ = setup_tools
    running, seen = set(), []

    async def handler(name, cid, args):
        if name == "ReadFile":
            assert "write" not in running
            running.add(cid)
            seen.append((cid, "start", len(running)))
            await asyncio.sleep(0.025 if cid == "a" else 0.005)
            running.remove(cid)
            if cid == "after":
                assert (tmp_path / "changed").exists()
        else:
            assert not running
            running.add("write")
            await asyncio.sleep(0.01)
            (tmp_path / "changed").write_text("yes")
            running.remove("write")
        seen.append((cid, "end", len(running)))
        return ToolResult(cid, name, "succeeded", cid)

    # Keep the actual scheduler/registry metadata; replace only external work.
    for tool in executor.registry.tools.values():
        tool_name = tool.name()

        async def invoke(cid, args, name=tool_name):
            return await handler(name, cid, args)

        tool.handler = invoke
    results = await executor.execute_batch(
        [
            call("ReadFile", {"path": "a"}, "a"),
            call("ReadFile", {"path": "b"}, "b"),
            call("WriteFile", {"path": "changed", "content": "yes"}, "write"),
            call("ReadFile", {"path": "changed"}, "after"),
        ]
    )
    assert [r.call_id for r in results] == ["a", "b", "write", "after"]
    assert any(count == 2 for _, phase, count in seen if phase == "start")
    assert seen.index(("write", "end", 0)) < seen.index(("after", "start", 1))


@pytest.mark.asyncio
async def test_cancel_pending_approval_prevents_following_write(tmp_path, setup_tools):
    executor, *_ = setup_tools
    entered = asyncio.Event()

    async def approve(request):
        entered.set()
        await asyncio.Event().wait()

    executor.approve = approve
    task = asyncio.create_task(
        executor.execute_batch(
            [
                call("Shell", {"command": "Write-Output 'no'"}, "shell"),
                call("WriteFile", {"path": "never", "content": "bad"}, "write"),
            ]
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path / "never").exists()


@pytest.mark.asyncio
async def test_real_read_write_read_sequence(tmp_path, setup_tools):
    executor, *_ = setup_tools
    (tmp_path / "a").write_text("before")
    (tmp_path / "b").write_text("other")
    results = await executor.execute_batch(
        [
            call("ReadFile", {"path": "a"}, "a"),
            call("ReadFile", {"path": "b"}, "b"),
            call("EditFile", {"path": "a", "old_text": "before", "new_text": "after"}, "edit"),
            call("ReadFile", {"path": "a"}, "last"),
        ]
    )
    assert all(r.status == "succeeded" for r in results)
    assert "after" in results[-1].content


@pytest.mark.asyncio
async def test_real_read_workers_overlap_before_write(tmp_path, setup_tools, monkeypatch):
    executor, *_ = setup_tools
    (tmp_path / "a").write_text("before")
    (tmp_path / "b").write_text("other")
    rendezvous = threading.Barrier(2)
    original = executor._read
    thread_ids = set()
    lock = threading.Lock()

    def read(path):
        # Exercise actual dispatch, fingerprinting, read and write code. Both reads
        # must enter worker threads together; serial async wrappers cannot pass.
        if path.name in {"a", "b"}:
            with lock:
                thread_ids.add(threading.get_ident())
            rendezvous.wait(timeout=3)
        return original(path)

    monkeypatch.setattr(executor, "_read", read)
    results = await executor.execute_batch(
        [
            call("ReadFile", {"path": "a"}, "read-a"),
            call("ReadFile", {"path": "b"}, "read-b"),
            call("WriteFile", {"path": "a", "content": "after"}, "write-a"),
        ]
    )
    assert all(result.status == "succeeded" for result in results)
    assert len(thread_ids) == 2
    assert (tmp_path / "a").read_text() == "after"


@pytest.mark.asyncio
async def test_cancel_read_waits_for_worker_before_releasing_group(
    tmp_path, setup_tools, monkeypatch
):
    executor, *_ = setup_tools
    (tmp_path / "a").write_text("before")
    entered, release = threading.Event(), threading.Event()
    original = executor._read

    def slow_read(path):
        entered.set()
        release.wait(timeout=5)
        return original(path)

    monkeypatch.setattr(executor, "_read", slow_read)
    task = asyncio.create_task(
        executor.execute_batch(
            [
                call("ReadFile", {"path": "a"}, "read"),
                call("WriteFile", {"path": "never", "content": "no"}, "write"),
            ]
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0.025)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path / "never").exists()
