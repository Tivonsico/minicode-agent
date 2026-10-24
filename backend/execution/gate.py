"""Conservative local mode: cooperative tool batches share one OS lock per user."""

import asyncio
import json
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from backend.conversation.snapshot import identity
from backend.observability.scope import current
from backend.storage.files import atomic_json, sync_directory
from backend.tools.base import ToolError


def lock_directory():
    owner = str(os.getuid()) if hasattr(os, "getuid") else "current-user"
    return Path(tempfile.gettempdir()) / f"minicode-execution-{owner}"


def mysql_domain_path():
    # Keep unknown MySQL coordination across reboot/temp-directory cleanup.
    return Path.home() / ".minicode" / "mysql-domain.json"


class ExecutionLease:
    def __init__(self, directory):
        self.path = directory / "unresolved.json"
        self.token = None

    def pending(self):
        if self.path.is_symlink():
            raise OSError("Execution marker must not be a link")
        if not self.path.exists():
            return None
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("journal"), str)
                or not isinstance(record.get("session_id"), str)
                or not isinstance(record.get("call_id"), str)
                or not isinstance(record.get("offset"), int)
                or not isinstance(record.get("seq"), int)
            ):
                raise ValueError("Invalid execution marker")
            return record
        except (ValueError, OSError) as error:
            raise ToolError(
                "共享执行状态损坏，必须人工核查后恢复。", "termination_unknown"
            ) from error

    def _remove(self):
        self.path.unlink(missing_ok=True)
        sync_directory(self.path.parent)

    def before_effect(self, store, cid):
        if store is None:
            return
        self.token = {
            "session_id": store.session_id,
            "call_id": cid,
            **current(store),
            "journal": str(store.path),
            "offset": store._offset,
            "seq": store.seq,
        }
        atomic_json(self.path, self.token)

    def completed(self):
        if self.token is not None and self.pending() == self.token:
            self._remove()
        self.token = None

    def clear_for(self, store, operation):
        pending = self.pending()
        if pending and (
            pending.get("session_id") == store.session_id
            and pending.get("journal") == str(store.path)
            and identity(pending, "call_id") == identity(operation, "call_id")
        ):
            self._remove()

    def clear_proven_result(self):
        """Repair the result-commit / marker-unlink crash window without rerunning."""
        pending = self.pending()
        if pending is None:
            return
        try:
            path = Path(pending["journal"])
            if path.is_symlink():
                return
            unknown_seq = None
            with path.open("rb") as file:
                file.seek(pending["offset"])
                expected = pending["seq"] + 1
                for line in file:
                    event = json.loads(line)
                    if event.get("session_id") != pending["session_id"] or event["seq"] != expected:
                        return
                    expected += 1
                    if identity(event, "call_id") != identity(pending, "call_id"):
                        continue
                    if event["type"] == "tool_result":
                        if event.get("status") not in (None, "unknown"):
                            self._remove()
                            return
                        unknown_seq = event["seq"]
                    elif (
                        event["type"] == "operation_reconciled"
                        and event.get("result_seq") == unknown_seq
                        and event.get("processes_stopped") is True
                    ):
                        self._remove()
                        return
        except (KeyError, TypeError, ValueError, OSError):
            return


@asynccontextmanager
async def execution_gate(*, reconciliation=False, shared=False, lock_name="tools.lock"):
    # A shared lock also covers overlapping/nested directories and separate app roots.
    # Read groups inside one batch may still run concurrently. Non-cooperating programs
    # remain outside this advisory protocol, so file fingerprint checks still apply.
    directory = lock_directory()
    if directory.is_symlink():
        raise OSError("Execution lock directory must not be a link")
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / lock_name
    if path.is_symlink():
        raise OSError("Execution lock must not be a link")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
        else:
            import fcntl
        while not acquired:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBRLCK if shared else msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                await asyncio.sleep(0.01)
            except OSError as error:
                if os.name == "nt" and error.errno in (13, 36):
                    await asyncio.sleep(0.01)
                else:
                    raise
        lease = ExecutionLease(directory)
        if not reconciliation:
            if not shared and mysql_domain_path().exists():
                raise ToolError(
                    "当前用户的 MySQL 执行域仍在使用或有待核实操作；请先在 MySQL 模式核实并正常关闭。",
                    "termination_unknown",
                )
            lease.clear_proven_result()
            if lease.pending() is not None:
                raise ToolError(
                    "存在未确认的文件或 Shell 操作；请在原会话核实并确认旧进程停止。",
                    "termination_unknown",
                )
        yield lease
    finally:
        if acquired:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
