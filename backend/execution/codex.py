"""Use the installed Codex sandbox, without calling a model or inheriting secrets."""

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

from backend.tools.base import ToolError

from .policy import sandbox_state
from .process import TerminationUnknown, capture
from .python_runtime import python_runtime


def sanitized_environment(application_root=None, python_environment=None, python_paths=()):
    allowed = {
        "PATH",
        "HOME",
        "USERPROFILE",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "PATHEXT",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    runtime = python_runtime(application_root)
    if runtime:
        environment["PATH"] = str(runtime[1].parent) + os.pathsep + environment.get("PATH", "")
    if python_environment:
        directory = Path(python_environment)
        binary = directory / ("Scripts" if os.name == "nt" else "bin")
        environment["PATH"] = str(binary) + os.pathsep + environment.get("PATH", "")
        environment["VIRTUAL_ENV"] = str(directory)
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        if python_paths:
            environment["PYTHONPATH"] = os.pathsep.join(map(str, python_paths))
    return environment


class CodexSandbox:
    def __init__(
        self,
        application_root,
        roots,
        executable=None,
        *,
        python_environment=None,
        runtime_roots=(),
        python_paths=(),
    ):
        self.application_root = Path(application_root).resolve()
        self.roots = roots
        self.python_environment = python_environment
        self.python_paths = tuple(python_paths)
        self.runtime_roots = tuple(runtime_roots)
        bundled = Path(
            "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
        )
        self.executable = (
            executable or os.environ.get("MINICODE_CODEX_PATH") or shutil.which("codex")
        )
        if not self.executable and bundled.is_file():
            self.executable = str(bundled)
        self._verified = False
        self.reason = "尚未验证沙箱"

    def available(self):
        return self._verified

    def argv(self, root, roots, command):
        return [
            self.executable,
            "sandbox",
            "--sandbox-state-json",
            json.dumps(sandbox_state(root, roots, self.application_root, self.runtime_roots)),
            "--",
            *command,
        ]

    async def verify(self):
        if not self.executable:
            self.reason = "未找到 Codex CLI；安装支持 sandbox-state-json 的版本后重启"
            return False
        try:
            with tempfile.TemporaryDirectory(prefix="minicode-sandbox-") as directory:
                parent = Path(directory).resolve()
                work = parent / "work"
                work.mkdir()
                secret = work / ".env"
                secret.write_text("sandbox-probe")
                outside = parent / "outside"
                outside.write_text("outside-probe")
                if os.name == "nt":
                    command = [
                        "powershell.exe",
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        "[IO.File]::WriteAllText('ok','ok'); $blocked=0; "
                        "foreach($pair in @(@('.env','r'),@('../outside','r'),@('../escape','w'))) { "
                        "try { if ($pair[1] -eq 'r') {[IO.File]::ReadAllText($pair[0]) | Out-Null} "
                        "else {[IO.File]::WriteAllText($pair[0],'bad')} } catch {$blocked++} }; "
                        "try { $c=[Net.Sockets.TcpClient]::new(); "
                        "$task=$c.ConnectAsync('1.1.1.1',443); if(-not $task.Wait(1500)) {throw 'timeout'}; "
                        "$c.Close() } catch {$blocked++}; Write-Output ('MINICODE_BLOCKED '+$blocked)",
                    ]
                else:
                    command = [
                        "/bin/sh",
                        "-c",
                        "printf ok > ok; blocked=0; "
                        "if ! cat .env >/dev/null 2>&1; then blocked=$((blocked+1)); fi; "
                        "if ! cat ../outside >/dev/null 2>&1; then blocked=$((blocked+1)); fi; "
                        "if ! (printf bad > ../escape) 2>/dev/null; then blocked=$((blocked+1)); fi; "
                        "if ! /usr/bin/curl --silent --connect-timeout 1 --max-time 2 "
                        "http://1.1.1.1 >/dev/null 2>&1; then blocked=$((blocked+1)); fi; "
                        "printf 'MINICODE_BLOCKED %s\\n' \"$blocked\"",
                    ]
                code, out, _ = await capture(
                    self.argv(work, [work], command),
                    work,
                    sanitized_environment(self.application_root),
                    12,
                )
                self._verified = (
                    code == 0 and "MINICODE_BLOCKED 4" in out and not (parent / "escape").exists()
                )
                self.reason = (
                    "已验证：工作区读写、范围外与密钥拒绝、网络关闭"
                    if self._verified
                    else "沙箱边界验证未通过；Shell 已禁用"
                )
        except (OSError, ValueError, TimeoutError, TerminationUnknown):
            self.reason = "Codex CLI 或本机沙箱不兼容；Shell 已禁用"
        return self._verified

    async def run(self, *, root, command, timeout, output_store, call_id, emit, executable=None):
        if not self.available():
            raise ToolError(self.reason, "shell_unavailable")
        roots = tuple(Path(value).resolve() for value in self.roots())
        root = Path(root).resolve()
        if root not in roots:
            raise ToolError("Shell work directory is no longer authorized.", "stale_approval")
        executable = executable or ("powershell.exe" if os.name == "nt" else "/bin/sh")
        if os.name != "nt" and Path(executable).name.lower() in {"powershell.exe", "cmd.exe"}:
            executable = "/bin/sh"  # Old Windows .env remains usable on macOS/Linux.
        if os.name == "nt" or Path(executable).name.lower() in {
            "pwsh",
            "pwsh.exe",
            "powershell.exe",
        }:
            command_args = [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); " + command,
            ]
        else:
            dialect = Path(executable).name.lower()
            if dialect in {"sh", "dash"}:
                executable, dialect = "/bin/bash", "bash"
            command_args = [executable]
            if dialect == "bash":
                command_args.extend(["--noprofile", "--norc"])
            if dialect in {"bash", "zsh", "ksh"}:
                command_args.extend(["-o", "pipefail"])
            command_args.extend(["-c", command])
        try:
            code, stdout, stderr = await capture(
                self.argv(root, roots, command_args),
                root,
                sanitized_environment(
                    self.application_root, self.python_environment, self.python_paths
                ),
                timeout,
            )
            # No raw stream reaches browser/model before redaction.
            content = f"exit_code={code}\n{stdout}" + (f"\n[stderr]\n{stderr}" if stderr else "")
            try:
                preview = output_store.preview(call_id, content)
            except (OSError, ValueError) as exc:
                raise ToolError(
                    "Shell ran but output could not be saved.", "storage_error"
                ) from exc
            return (
                "succeeded" if code == 0 else "failed",
                preview,
                None if code == 0 else "exit_code",
            )
        except TerminationUnknown as exc:
            raise ToolError(str(exc), "termination_unknown") from exc
        except TimeoutError:
            return "failed", "Shell timed out; child processes were stopped.", "timeout"
        except ValueError as exc:
            return "failed", str(exc), "output_limit"
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            raise ToolError(
                f"Sandbox process unavailable: {type(exc).__name__}", "shell_unavailable"
            ) from exc
