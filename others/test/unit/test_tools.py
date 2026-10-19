"""Guard boundaries: traversal, stale reads, and changed approval must not mutate files."""

import asyncio
import os
import subprocess
import time

import pytest

from backend.tools.base import ToolError
from test.support.tooling import call


@pytest.mark.asyncio
async def test_write_requires_current_read_and_invalidates_after_edit(tmp_path, setup_tools):
    executor, *_ = setup_tools
    path = tmp_path / "a.txt"
    path.write_text("original", encoding="utf-8")
    result = await executor.execute_batch([call("WriteFile", {"path": "a.txt", "content": "oops"})])
    assert result[0].error_type == "stale_read"
    await executor.execute_batch([call("ReadFile", {"path": "a.txt"})])
    path.write_text("external", encoding="utf-8")
    result = await executor.execute_batch([call("WriteFile", {"path": "a.txt", "content": "oops"})])
    assert result[0].error_type == "stale_read"
    assert path.read_text() == "external"
    await executor.execute_batch([call("ReadFile", {"path": "a.txt"})])
    result = await executor.execute_batch(
        [call("EditFile", {"path": "a.txt", "old_text": "external", "new_text": "saved"})]
    )
    assert result[0].status == "succeeded"
    assert path.read_text() == "saved"
    result = await executor.execute_batch([call("WriteFile", {"path": "a.txt", "content": "oops"})])
    assert result[0].error_type == "stale_read"


@pytest.mark.asyncio
async def test_create_ambiguous_edit_and_reset(tmp_path, setup_tools):
    executor, *_ = setup_tools
    results = await executor.execute_batch(
        [
            call("WriteFile", {"path": "sub/a.txt", "content": "x x"}),
            call("ReadFile", {"path": "sub/a.txt"}),
        ]
    )
    assert all(r.status == "succeeded" for r in results)
    result = await executor.execute_batch(
        [call("EditFile", {"path": "sub/a.txt", "old_text": "x", "new_text": "y"})]
    )
    assert result[0].error_type == "ambiguous_edit"
    executor.reset_turn()
    result = await executor.execute_batch(
        [call("WriteFile", {"path": "sub/a.txt", "content": "no"})]
    )
    assert result[0].error_type == "stale_read"


@pytest.mark.asyncio
async def test_delete_bound_to_version_and_reject_directory(tmp_path, setup_tools):
    executor, *_ = setup_tools
    path = tmp_path / "a.txt"
    path.write_text("before")

    async def changed(request):
        path.write_text("after")
        return True

    executor.approve = changed
    result = await executor.execute_batch([call("DeleteFile", {"path": "a.txt"})])
    assert result[0].error_type == "stale_approval"
    assert path.read_text() == "after"
    result = await executor.execute_batch([call("DeleteFile", {"path": "."})])
    assert result[0].error_type == "not_file"


@pytest.mark.asyncio
async def test_path_boundary_sensitive_and_internal_outputs(tmp_path, setup_tools):
    executor, output, *_ = setup_tools
    (tmp_path / ".env").write_text("SECRET")
    (tmp_path / ".env.local").write_text("SECRET")
    (tmp_path / ".env.example").write_text("KEY=placeholder")
    (tmp_path / "hidden.txt").write_text("-----BEGIN PRIVATE KEY-----\nSECRET")
    for target in [
        "../escape",
        str(tmp_path.parent / "outside.txt"),
        ".env",
        ".env.local",
        ".minicode/sessions/test",
        "hidden.txt",
    ]:
        result = await executor.execute_batch([call("ReadFile", {"path": target})])
        assert result[0].status == "failed"
        assert "SECRET" not in result[0].content
        executor.reset_turn()
    assert (await executor.execute_batch([call("ReadFile", {"path": ".env.example"})]))[
        0
    ].status == "succeeded"
    saved = output.save("log", "line one\nline two")
    result = await executor.execute_batch(
        [call("ReadFile", {"path": saved["path"], "start_line": 2, "line_count": 1})]
    )
    assert "2: line two" in result[0].content
    result = await executor.execute_batch(
        [call("WriteFile", {"path": saved["path"], "content": "no"})]
    )
    assert result[0].status == "failed"
    result = await executor.execute_batch([call("Grep", {"pattern": "SECRET"})])
    assert "SECRET" not in result[0].content


def test_path_symlink_escape(tmp_path, setup_tools):
    executor, *_ = setup_tools
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    link = tmp_path / "alias"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("Creating symlinks requires privilege")
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, check=False
        )
        if created.returncode:
            pytest.skip("Creating a Windows junction was denied")
    with pytest.raises(ToolError):
        executor.policy.resolve("alias/new.txt")


@pytest.mark.asyncio
async def test_schema_and_malformed_calls_do_not_write(tmp_path, setup_tools):
    executor, *_ = setup_tools
    calls = [
        call("WriteFile", {"path": "a.txt", "content": "x", "unexpected": True}),
        {"id": "broken", "function": {"name": "WriteFile", "arguments": '{"path":'}},
        call("Unknown", {}),
        call("ReadFile", {"path": "a.txt", "start_line": 0}),
    ]
    results = await executor.execute_batch(calls)
    assert all(r.status in {"failed", "cancelled"} for r in results)
    assert not (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
async def test_rejected_operation_stops_current_batch(tmp_path, setup_tools):
    executor, _, approvals, _ = setup_tools

    async def deny(request):
        approvals.append(request)
        return False

    executor.approve = deny
    (tmp_path / "a.txt").write_text("keep")
    results = await executor.execute_batch(
        [call("DeleteFile", {"path": "a.txt"}, str(i)) for i in range(4)]
    )
    assert results[0].status == "rejected"
    assert all(r.status == "cancelled" for r in results[1:])
    assert len(approvals) == 1
    assert executor.terminal_failed and executor.pause_reason == "user_rejected"
    assert (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
async def test_three_failures_pause_before_write(tmp_path, setup_tools):
    executor, *_ = setup_tools
    calls = [
        call("EditFile", {"path": "absent", "old_text": "x", "new_text": "y"}, str(i))
        for i in range(3)
    ]
    calls.append(call("WriteFile", {"path": "never", "content": "bad"}, "last"))
    results = await executor.execute_batch(calls)
    assert executor.terminal_failed
    assert results[-1].status == "cancelled"
    assert not (tmp_path / "never").exists()


async def test_delete_binary_single_file_after_approval(tmp_path, setup_tools):
    executor, _, approvals, _ = setup_tools
    target = tmp_path / "image.bin"
    target.write_bytes(b"\x00\xff\x80binary")
    results = await executor.execute_batch([call("DeleteFile", {"path": "image.bin"})])
    assert results[0].status == "succeeded"
    assert len(approvals) == 1
    assert not target.exists()


@pytest.mark.asyncio
async def test_write_approval_is_required_and_bound_to_version(tmp_path, setup_tools):
    executor, _, approvals, _ = setup_tools
    target = tmp_path / "a.txt"
    target.write_text("original", encoding="utf-8")
    await executor.execute_batch([call("ReadFile", {"path": "a.txt"})])

    async def changed(request):
        approvals.append(request)
        target.write_text("external", encoding="utf-8")
        return True

    executor.approve = changed
    result = await executor.execute_batch(
        [call("WriteFile", {"path": "a.txt", "content": "replacement"})]
    )
    assert result[0].error_type == "stale_approval"
    assert target.read_text(encoding="utf-8") == "external"
    assert "original" in approvals[0]["details"]
    assert "replacement" in approvals[0]["details"]


@pytest.mark.asyncio
async def test_each_change_to_same_file_needs_new_approval(tmp_path, setup_tools):
    executor, _, approvals, _ = setup_tools
    target = tmp_path / "note.txt"
    target.write_text("one", encoding="utf-8")
    await executor.execute_batch([call("ReadFile", {"path": "note.txt"}, "read-1")])
    first = await executor.execute_batch(
        [call("WriteFile", {"path": "note.txt", "content": "two"}, "write-1")]
    )
    await executor.execute_batch([call("ReadFile", {"path": "note.txt"}, "read-2")])
    second = await executor.execute_batch(
        [call("WriteFile", {"path": "note.txt", "content": "three"}, "write-2")]
    )
    assert first[0].status == second[0].status == "succeeded"
    assert target.read_text(encoding="utf-8") == "three"
    assert len(approvals) == 2
    assert "two" in approvals[0]["details"] and "three" in approvals[1]["details"]


@pytest.mark.asyncio
async def test_shell_blacklist_runs_before_sandbox_and_approval(tmp_path, setup_tools):
    executor, _, approvals, _ = setup_tools
    rejected = await executor.execute_batch([call("Shell", {"command": "DROP DATABASE prod"})])
    assert rejected[0].error_type == "dangerous_command"
    assert not approvals
    unavailable = await executor.execute_batch([call("Shell", {"command": "git status"})])
    assert unavailable[0].error_type == "shell_unavailable"
    assert not approvals


def test_hard_link_alias_is_not_readable(tmp_path, setup_tools):
    executor, *_ = setup_tools
    secret = tmp_path / ".env"
    secret.write_text("SECRET", encoding="utf-8")
    alias = tmp_path / "ordinary.txt"
    try:
        os.link(secret, alias)
    except OSError:
        pytest.skip("Hard links unavailable")
    with pytest.raises(ToolError) as error:
        executor.policy.resolve("ordinary.txt")
    assert error.value.error_type == "sensitive"


@pytest.mark.asyncio
async def test_glob_star_stays_at_one_level_and_lists_directories(tmp_path, setup_tools):
    executor, *_ = setup_tools
    (tmp_path / "top.txt").write_text("top", encoding="utf-8")
    subdir = tmp_path / "folder"
    subdir.mkdir()
    (subdir / "child.txt").write_text("child", encoding="utf-8")
    deep = subdir / "deep"
    deep.mkdir()
    (deep / "grandchild.txt").write_text("deep", encoding="utf-8")

    top = await executor.execute_batch([call("Glob", {"pattern": "*"}, "top")])
    assert set(top[0].content.splitlines()) == {"top.txt", "folder/"}
    child = await executor.execute_batch([call("Glob", {"pattern": "*/*"}, "child")])
    assert set(child[0].content.splitlines()) == {"folder/child.txt", "folder/deep/"}
    recursive = await executor.execute_batch([call("Glob", {"pattern": "**/*.txt"}, "recursive")])
    assert set(recursive[0].content.splitlines()) == {
        "top.txt",
        "folder/child.txt",
        "folder/deep/grandchild.txt",
    }


@pytest.mark.asyncio
async def test_slow_search_does_not_block_ui_and_cancels_promptly(
    tmp_path, setup_tools, monkeypatch
):
    executor, *_ = setup_tools
    original = executor.policy.files

    def slow_files(**kwargs):
        time.sleep(0.25)
        yield from original(**kwargs)

    monkeypatch.setattr(executor.policy, "files", slow_files)
    task = asyncio.create_task(executor.execute_batch([call("Glob", {"pattern": "*"}, "slow")]))
    await asyncio.sleep(0.02)
    assert not task.done()
    started = asyncio.get_running_loop().time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert asyncio.get_running_loop().time() - started < 0.15
