import asyncio
import shutil

import pytest

from test.support.tooling import call
from tools import shell

pytestmark = pytest.mark.skip(reason="Shell execution requires a verified Windows sandbox")


@pytest.mark.asyncio
async def test_shell_output_exit_code_and_approval(tmp_path, setup_tools):
    executor, output, approvals, events = setup_tools
    results = await executor.execute_batch(
        [
            call(
                "Shell",
                {"command": "Write-Output 'hello'; [Console]::Error.WriteLine('problem'); exit 7"},
            )
        ]
    )
    assert results[0].status == "failed"
    assert results[0].error_type == "exit_code"
    assert "hello" in results[0].content and "problem" in results[0].content
    assert str(tmp_path) in approvals[0]["description"]
    assert "120" in approvals[0]["description"]
    assert len(output.records) == 2
    assert any(kind == "shell_result" and value["exit_code"] == 7 for kind, value in events)


@pytest.mark.asyncio
async def test_shell_timeout_stops_before_side_effect(tmp_path, setup_tools):
    executor, _, approvals, _ = setup_tools
    result = await executor.execute_batch(
        [
            call(
                "Shell",
                {
                    "command": "Start-Sleep -Seconds 5; Set-Content -Path late.txt -Value bad",
                    "timeout": 0.3,
                },
            )
        ]
    )
    assert result[0].error_type in {"timeout", "termination_unknown"}
    assert not (tmp_path / "late.txt").exists()
    assert "0.3" in approvals[0]["description"]


@pytest.mark.asyncio
async def test_cancel_running_shell_records_and_no_next_tool(tmp_path, setup_tools):
    executor, output, _, events = setup_tools
    started = asyncio.Event()
    original = executor.emit

    def emit(kind, **data):
        original(kind, **data)
        if kind == "tool_running":
            started.set()

    executor.emit = emit
    task = asyncio.create_task(
        executor.execute_batch(
            [
                call(
                    "Shell",
                    {
                        "command": "Write-Output started; Start-Sleep -Seconds 10; Set-Content late.txt bad"
                    },
                    "shell",
                ),
                call("WriteFile", {"path": "never.txt", "content": "bad"}, "write"),
            ]
        )
    )
    await started.wait()
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path / "never.txt").exists()
    assert not (tmp_path / "late.txt").exists()
    assert output.records
    assert any(
        kind == "tool_result" and data["status"] in {"cancelled", "unknown"}
        for kind, data in events
    )


@pytest.mark.asyncio
async def test_spool_allocation_failure_never_starts_child(tmp_path, setup_tools, monkeypatch):
    executor, *_ = setup_tools

    async def forbidden_spawn(*args, **kwargs):
        raise AssertionError("A process was started before output storage was ready")

    def failed_storage(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", forbidden_spawn)
    monkeypatch.setattr(shell.tempfile, "TemporaryFile", failed_storage)
    result = await executor.execute_batch(
        [
            call("Shell", {"command": "Write-Output hello"}),
            call("WriteFile", {"path": "never", "content": "no"}, "write"),
        ]
    )
    assert result[0].error_type == "storage_error"
    assert executor.terminal_failed
    assert not (tmp_path / "never").exists()


@pytest.mark.asyncio
async def test_spool_write_failure_terminates_running_child(tmp_path, setup_tools, monkeypatch):
    executor, *_ = setup_tools
    original = shell.tempfile.TemporaryFile

    class FailedSpool:
        def __init__(self, *args, **kwargs):
            self.file = original(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def __getattr__(self, name):
            return getattr(self.file, name)

        def write(self, content):
            raise OSError("disk full")

    monkeypatch.setattr(shell.tempfile, "TemporaryFile", FailedSpool)
    result = await executor.execute_batch(
        [
            call(
                "Shell",
                {
                    "command": "Write-Output hello; Start-Sleep -Seconds 10; Set-Content late.txt bad"
                },
            ),
            call("WriteFile", {"path": "never", "content": "no"}, "write"),
        ]
    )
    assert result[0].error_type == "storage_error"
    assert executor.terminal_failed
    assert not (tmp_path / "late.txt").exists()
    assert not (tmp_path / "never").exists()


@pytest.mark.asyncio
async def test_configured_shell_executable_is_used(tmp_path, setup_tools, monkeypatch):
    executor, *_ = setup_tools
    seen = []
    original = shell.asyncio.create_subprocess_exec
    executable = shutil.which("powershell") or shutil.which("pwsh")
    executor.shell_executable = executable

    async def spawn(program, *args, **kwargs):
        seen.append(program)
        return await original(program, *args, **kwargs)

    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", spawn)
    result = await executor.execute_batch([call("Shell", {"command": "Write-Output yes"})])
    assert result[0].status == "succeeded"
    assert seen[0] == executable


@pytest.mark.asyncio
async def test_powershell_chinese_output_is_utf8(tmp_path, setup_tools):
    executor, _, approvals, _ = setup_tools
    executor.shell_executable = shutil.which("powershell") or shutil.which("pwsh")
    result = await executor.execute_batch(
        [
            call(
                "Shell",
                {
                    "command": "Write-Output '你好，minicode'; [Console]::Error.WriteLine('错误详情')"
                },
            )
        ]
    )
    assert result[0].status == "succeeded"
    assert "你好，minicode" in result[0].content
    assert "错误详情" in result[0].content
    assert shell.UTF8_PRELUDE in approvals[0]["description"]
