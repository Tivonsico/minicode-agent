"""Real installed Codex sandbox, no LLM and no unsafe host-shell fallback."""

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

from backend.context.prompts import assemble_system_prompt
from backend.conversation.store import SessionStore
from backend.execution.codex import CodexSandbox
from backend.permissions.directories import SessionPermissions
from backend.storage.output_store import OutputStore
from backend.tools.executor import ToolExecutor
from test.support.tooling import call


async def verified(tmp_path):
    application = tmp_path / "app"
    work = tmp_path / "work"
    application.mkdir()
    work.mkdir()
    backend = CodexSandbox(application, lambda: (work,))
    if not await backend.verify():
        pytest.skip(backend.reason)
    return backend, work, OutputStore(SessionStore(application))


@pytest.mark.skipif(os.name == "nt", reason="POSIX command fixture")
@pytest.mark.parametrize("changed", [False, True])
async def test_shell_preserves_unchanged_read_but_changed_file_still_rejects_edit(
    tmp_path, changed
):
    backend, work, output = await verified(tmp_path)
    grants = SessionPermissions(backend.application_root, output.store)
    grants.add(str(work))

    async def approve(_request):
        return True

    executor = ToolExecutor(backend.application_root, output, approve, session_permissions=grants)
    executor.agent_mode = "auto"
    executor.shell_backend = backend
    executor.begin_turn(str(work))
    assemble_system_prompt(executor, SimpleNamespace(snapshot=lambda: []))
    target = work / "state.py"
    target.write_text("x = 1\n")
    await executor.execute_batch([call("ReadFile", {"path": str(target)}, "read")])
    command = "printf 'x = 3\\n' > state.py" if changed else "printf inspection"
    shell = await executor.execute_batch([call("Shell", {"command": command}, "shell")])
    assert shell[0].status == "succeeded", shell[0].content
    result = await executor.execute_batch(
        [
            call(
                "EditFile",
                {
                    "path": str(target),
                    "old_text": "x = 1",
                    "new_text": "x = 2",
                },
                "edit",
            )
        ]
    )
    assert result[0].status == ("failed" if changed else "succeeded")
    assert result[0].error_type == ("stale_read" if changed else None)
    assert target.read_text() == ("x = 3\n" if changed else "x = 2\n")


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX command fixture; Windows policy needs platform CI"
)
async def test_real_sandbox_workspace_secret_outside_network_and_environment(tmp_path, monkeypatch):
    backend, work, output = await verified(tmp_path)
    (work / ".env").write_text("SECRET")
    (tmp_path / "outside").write_text("OUTSIDE")
    monkeypatch.setenv("OPENAI_API_KEY", "DO_NOT_INHERIT")
    status, content, _ = await backend.run(
        root=work,
        command="echo ok > allowed; cat .env; cat ../outside; echo bad > ../escape; printenv OPENAI_API_KEY",
        timeout=5,
        output_store=output,
        call_id="boundary",
        emit=lambda *a, **k: None,
    )
    assert (work / "allowed").read_text().strip() == "ok"
    assert not (tmp_path / "escape").exists()
    assert "SECRET" not in content and "OUTSIDE" not in content and "DO_NOT_INHERIT" not in content
    assert status == "failed"


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX command fixture; Windows policy needs platform CI"
)
async def test_shell_timeout_and_cancel_settle_children(tmp_path):
    backend, work, output = await verified(tmp_path)
    status, _, reason = await backend.run(
        root=work,
        command="sleep 10; echo bad > timed",
        timeout=0.15,
        output_store=output,
        call_id="timeout",
        emit=lambda *a, **k: None,
    )
    assert status == "failed" and reason == "timeout"
    task = asyncio.create_task(
        backend.run(
            root=work,
            command="sleep 1; echo bad > cancelled",
            timeout=10,
            output_store=output,
            call_id="cancel",
            emit=lambda *a, **k: None,
        )
    )
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.1)
    assert not (work / "timed").exists() and not (work / "cancelled").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fixture")
async def test_shell_python_uses_actual_runtime_without_broadening_boundaries(
    tmp_path, monkeypatch
):
    backend, work, output = await verified(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    (backend.application_root / "private.txt").write_text("PRIVATE")
    (tmp_path / "outside").write_text("OUTSIDE")
    (work / ".env").write_text("SECRET")
    program = (
        "import sys; from pathlib import Path; "
        "Path('version.txt').write_text(sys.executable); "
        f"paths = {list(map(str, [work / '.env', tmp_path / 'outside', backend.application_root / 'private.txt']))!r}; "
        "blocked=0\n"
        "for p in paths:\n"
        " try: Path(p).read_text()\n"
        " except PermissionError: blocked += 1\n"
        "assert blocked == 3\n"
        "print('PYTHON_OK')\n"
    )
    import shlex

    status, content, _ = await backend.run(
        root=work,
        command="python3 -c " + shlex.quote(program),
        timeout=5,
        output_store=output,
        call_id="python",
        emit=lambda *a, **k: None,
    )
    assert status == "succeeded", content
    assert "PYTHON_OK" in content
    from pathlib import Path

    assert Path((work / "version.txt").read_text()).resolve() == Path(sys.executable).resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fixture")
async def test_pinned_self_test_python_is_read_only_and_preserves_secret_boundaries(tmp_path):
    import shlex
    from pathlib import Path

    from backend.execution.process import capture

    backend, work, output = await verified(tmp_path)
    environment = tmp_path / "self-test-env"
    code, _, err = await capture(
        [sys.executable, "-m", "venv", "--without-pip", str(environment)], tmp_path, {}, 20
    )
    assert code == 0, err
    # Use the concrete interpreter, without granting the whole uv installation.
    binary = environment / "bin/python3"
    target = binary.resolve()
    binary.unlink()
    binary.symlink_to(target)
    config = environment / "pyvenv.cfg"
    config.write_text(
        "\n".join(
            "home = " + str(Path(line.split("=", 1)[1].strip()).resolve())
            if line.startswith("home =")
            else line
            for line in config.read_text().splitlines()
        )
        + "\n"
    )
    packages = next((environment / "lib").glob("python*/site-packages"))
    (packages / "self_test_fixture.py").write_text("MARKER = 'PINNED'\n")
    (work / ".env").write_text("SECRET")
    (tmp_path / "outside").write_text("OUTSIDE")
    backend.python_environment = environment
    backend.runtime_roots = (environment,)
    program = (
        "import sys; from pathlib import Path; import self_test_fixture; "
        f"assert Path(sys.prefix) == Path({str(environment)!r}); "
        "assert self_test_fixture.MARKER == 'PINNED'; blocked=0\n"
        f"for path, action in {[(str(work / '.env'), 'read'), (str(tmp_path / 'outside'), 'read'), (str(packages / 'self_test_fixture.py'), 'write')]!r}:\n"
        " try:\n"
        "  p=Path(path); p.read_text() if action=='read' else p.write_text('tampered')\n"
        " except PermissionError: blocked+=1\n"
        "assert blocked==3; print('PINNED_AND_ISOLATED')"
    )
    status, content, _ = await backend.run(
        root=work,
        command="python3 -c " + shlex.quote(program),
        timeout=10,
        output_store=output,
        call_id="pinned-python",
        emit=lambda *a, **k: None,
    )
    assert status == "succeeded", content
    assert "PINNED_AND_ISOLATED" in content


@pytest.mark.skipif(os.name == "nt", reason="POSIX pipeline fixture")
async def test_pipeline_preserves_failed_python_status(tmp_path):
    backend, work, output = await verified(tmp_path)
    status, content, reason = await backend.run(
        root=work,
        command="python3 -c \"raise RuntimeError('REPRODUCED_FAILURE')\" 2>&1 | tail -2",
        timeout=10,
        output_store=output,
        call_id="pipeline",
        emit=lambda *a, **k: None,
    )
    assert status == "failed" and reason == "exit_code"
    assert "exit_code=1" in content and "REPRODUCED_FAILURE" in content
