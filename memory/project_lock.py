"""A held OS lock, not a stale PID flag."""

import os
from pathlib import Path

from memory.session_store import internal_directory


class ProjectLock:
    def __init__(self, root: Path):
        self.path = internal_directory(root) / "project.lock"
        self.file = None

    def acquire(self):
        if self.path.is_symlink():
            raise ValueError("Lock file must not be a link")
        file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            file.close()
            raise RuntimeError("该项目已有 minicode 实例正在运行") from exc
        self.file = file
        return self

    def release(self):
        if self.file:
            self.file.close()
            self.file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()
