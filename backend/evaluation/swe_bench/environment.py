"""An isolated, pinned official harness; no model credentials in its processes."""

import asyncio
import json
import os
from pathlib import Path

from backend.execution.process import capture

VERSION = "5.0.2"
DATASETS = {
    "swe_lite": "SWE-bench/SWE-bench_Lite",
    "swe_verified": "SWE-bench/SWE-bench_Verified",
}
SOURCE = "https://github.com/SWE-bench/swe-bench-tasks.git"


class Environment:
    def __init__(self, root):
        self.directory = Path(root) / ".minicode" / "swe-bench"
        self.directory.mkdir(parents=True, exist_ok=True)
        executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        self.python = Path(os.getenv("SWE_BENCH_PYTHON") or self.directory / "venv" / executable)
        self.repository = Path(os.getenv("SWE_BENCH_TASK_REPO") or self.directory / "task-repo")

    def variables(self):
        allowed = (
            "PATH",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        )
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        runtime = self.directory / "runtime"
        env["PATH"] = str(runtime / "bin") + os.pathsep + env.get("PATH", "")
        host = os.getenv("SWE_BENCH_DOCKER_HOST") or os.getenv("DOCKER_HOST")
        socket = self.directory / "lima" / "swe" / "sock" / "docker.sock"
        if host or socket.exists():
            env["DOCKER_HOST"] = host or f"unix://{socket}"
        env["DOCKER_CONFIG"] = str(runtime / "docker-config")
        env["LIMA_HOME"] = str(self.directory / "lima")
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_NO_LAZY_FETCH"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        return env

    async def command(self, action, *args, cwd=None, timeout=60):
        if not self.python.is_file():
            raise ValueError("请先安装独立 SWE-bench 运行环境，见官方评测接入说明")
        worker = Path(__file__).with_name("worker.py")
        code, out, err = await capture(
            [str(self.python), str(worker), action, *map(str, args)],
            cwd or self.directory,
            self.variables(),
            timeout,
        )
        if code:
            raise ValueError((err or out)[-3000:] or "官方验收器未正常结束")
        return json.loads(out)

    async def readiness(self):
        result = {
            "harness_version": VERSION,
            "python_ready": self.python.is_file(),
            "task_repository_ready": (self.repository / "sweb.yaml").is_file(),
        }
        try:
            result.update(await self.command("ready", timeout=15))
        except (OSError, ValueError, TimeoutError) as exc:
            result.update(docker_ready=False, reason=str(exc))
        result["ready"] = (
            result.get("docker_ready", False)
            and result["task_repository_ready"]
            and result.get("version") == VERSION
        )
        return result

    async def logged(self, action, args, cwd, timeout):
        """Stream verbose builds to disk, while bounding output and terminating children."""
        from backend.execution.process import stop_tree

        log = Path(cwd) / f"{action}.log"
        process = await asyncio.create_subprocess_exec(
            str(self.python),
            str(Path(__file__).with_name("worker.py")),
            action,
            *map(str, args),
            cwd=cwd,
            env=self.variables(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **({"start_new_session": True} if os.name != "nt" else {}),
        )
        try:
            async with asyncio.timeout(timeout):
                with log.open("wb") as stream:
                    total = 0
                    while chunk := await process.stdout.read(8192):
                        total += len(chunk)
                        if total > 20_000_000:
                            raise ValueError("官方验收日志超过20 MB，已停止本次运行")
                        stream.write(chunk)
                        stream.flush()
                    code = await process.wait()
                if code:
                    raise ValueError(f"官方 {action} 退出码 {code}，请查看验收日志")
        except BaseException:
            await asyncio.shield(stop_tree(process))
            raise
