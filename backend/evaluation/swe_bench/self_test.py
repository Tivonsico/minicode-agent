"""Pinned native self-test dependencies, verified before any model request."""

import asyncio
import json
import os
import shlex
import shutil
import time
from pathlib import Path

from backend.execution.codex import CodexSandbox, sanitized_environment
from backend.execution.process import capture
from backend.observability.inputs import digest

LOCK = asyncio.Lock()
PYTHON = "3.9.25"


def profile(task):
    """Configuration is checked in; unknown versions fail before model calls."""
    profiles = json.loads(Path(__file__).with_name("self_test_profiles.json").read_text())
    key = task["repo"] + "@" + task["version"]
    if key not in profiles["versions"]:
        raise ValueError("此仓库版本尚未配置自测依赖；请先配置并验证环境再运行模型")
    name = profiles.get("instances", {}).get(task.get("instance_id"), profiles["versions"][key])
    return name, profiles["profiles"][name]


def interpreter(directory):
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


async def prepare(env, task, work, artifact, verified_sandbox):
    """Only dependency setup can use networking; model tools remain offline."""
    name, configuration = profile(task)
    dependencies = configuration["dependencies"]
    package = configuration["package"]
    started = time.monotonic()
    directory = env.directory / "self-test" / name
    python = interpreter(directory)
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    async with LOCK:
        commands = (
            []
            if python.is_file()
            else [[uv, "venv", "--no-project", "--python", PYTHON, str(directory)]]
        )
        commands.append([uv, "pip", "install", "--python", str(python), *dependencies])
        with (artifact / "dependency-install.log").open("w") as log:
            for command in commands:
                code, out, err = await capture(command, env.directory, env.variables(), 600)
                log.write(out + err)
                if code:
                    raise ValueError("自测依赖安装失败，请查看 dependency-install.log")
        if configuration.get("install_editable"):
            # Normal package setup generates ignored version metadata and CLI
            # entry points. It is done on the pristine checkout before the model.
            variables = env.variables()
            variables["SETUPTOOLS_SCM_PRETEND_VERSION"] = configuration["distribution_version"]
            command = [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-build-isolation",
                "--editable",
                str(work),
            ]
            code, out, err = await capture(command, work, variables, 120)
            with (artifact / "dependency-install.log").open("a") as log:
                log.write(out + err)
            if code:
                raise ValueError("候选包原生安装失败，请查看 dependency-install.log")
        # Managed Python aliases can traverse an ungranted sibling symlink.
        # Pin the actual executable and stdlib home rather than broadening reads.
        if python.is_symlink():
            target = python.resolve(strict=True)
            python.unlink()
            python.symlink_to(target)
        config = directory / "pyvenv.cfg"
        lines = config.read_text().splitlines()
        config.write_text(
            "\n".join(
                "home = " + str(Path(line.split("=", 1)[1].strip()).resolve())
                if line.startswith("home =")
                else line
                for line in lines
            )
            + "\n"
        )
    code, out, err = await capture(
        [str(python), "-c", "import sys; print(sys.base_prefix)"], work, env.variables(), 15
    )
    if code:
        raise ValueError("自测解释器无法启动：" + err[-1000:])
    base = Path(out.strip()).resolve()
    python_paths = tuple(work / path for path in configuration.get("source_paths", []))
    backend = CodexSandbox(
        artifact / "runtime",
        lambda: (work,),
        verified_sandbox.executable,
        python_environment=directory,
        runtime_roots=(directory, base),
        python_paths=python_paths,
    )
    program = (
        "import sys,json,importlib.metadata as m; from pathlib import Path; "
        f"import {package} as candidate,pytest,distutils.version; "
        f"assert sys.version.split()[0] == {PYTHON!r}; "
        f"assert Path(sys.prefix).resolve() == Path({str(directory)!r}).resolve(); "
        "assert Path(candidate.__file__).resolve().is_relative_to(Path.cwd()); "
        + configuration["smoke"]
        + "; "
        f"expected={dict(p.split('==') for p in dependencies)!r}; "
        "actual={p:m.version(p) for p in expected}; assert actual==expected; "
        "print(json.dumps({'python':sys.version.split()[0], 'executable':sys.executable, "
        "'candidate_version':getattr(candidate,'__version__',None),"
        "'candidate_source':candidate.__file__, 'packages':actual}))"
    )
    aliases = f"import sys; assert sys.prefix == {str(directory)!r}"
    command = (
        [str(python), "-c", program]
        if os.name == "nt"
        else [
            "/bin/sh",
            "-c",
            "python -c "
            + shlex.quote(program)
            + " && python3 -c "
            + shlex.quote(aliases)
            + " && pytest --version >&2",
        ]
    )
    code, out, err = await capture(
        backend.argv(work, [work], command),
        work,
        sanitized_environment(backend.application_root, directory, python_paths),
        30,
    )
    (artifact / "self-test-preflight.log").write_text(out + err)
    if code:
        raise ValueError("自测环境沙箱预检失败；未请求模型，请查看 self-test-preflight.log")
    result = {
        **json.loads(out),
        "environment": str(directory),
        "profile": name,
        "profile_digest": digest(configuration),
        "python_paths": list(map(str, python_paths)),
        "base_runtime": str(base),
        "status": "ready",
        "setup_duration_ms": round((time.monotonic() - started) * 1000),
    }
    (artifact / "self-test.json").write_text(json.dumps(result, ensure_ascii=False))
    return result
