"""Stop only the process holding this project's runtime lock and its children."""

import json
import os
import time
from pathlib import Path

import psutil

from backend.storage.project_lock import ProjectLock


def _owner(path, root):
    try:
        owner = json.loads(path.read_text())
        if (
            not isinstance(owner, dict)
            or owner.get("project") != str(root)
            or not isinstance(owner.get("pid"), int)
            or owner["pid"] <= 0
            or not isinstance(owner.get("created_at"), (int, float))
        ):
            raise ValueError("Invalid owner")
        return owner
    except (ValueError, OSError):
        # Upgrade the original lock format, which stored only a single byte.
        candidates = []
        for process in psutil.process_iter():
            if process.pid == os.getpid():
                continue
            try:
                if any(Path(file.path) == path for file in process.open_files()):
                    candidates.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if len(candidates) != 1:
            raise RuntimeError("无法确认项目旧进程的身份，未终止任何进程")
        process = candidates[0]
        return {"pid": process.pid, "created_at": process.create_time()}


def stop(root):
    root = Path(root).resolve()
    path = root / ".minicode" / "project.lock"
    if not path.exists():
        return False
    lock = ProjectLock(root)
    try:
        lock.acquire()
    except RuntimeError:
        pass
    else:
        lock.release()
        return False  # An unlocked file is stale; never kill its recorded PID.

    owner = _owner(path, root)
    if owner["pid"] == os.getpid():
        raise RuntimeError("不能终止启动脚本自身")
    try:
        process = psutil.Process(owner["pid"])
        if abs(process.create_time() - owner["created_at"]) > 0.001:
            raise RuntimeError("进程身份已变化，未终止任何进程")
        children = process.children(recursive=True)
        for target in [*reversed(children), process]:
            try:
                target.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs([*children, process], timeout=3)
    except psutil.NoSuchProcess:
        pass
    except psutil.AccessDenied as error:
        raise RuntimeError("没有权限终止当前项目进程") from error

    deadline = time.monotonic() + 5
    while True:
        try:
            lock.acquire()
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise RuntimeError("旧进程仍占用项目锁，未启动新进程") from None
            time.sleep(0.05)
        else:
            lock.release()
            return True
