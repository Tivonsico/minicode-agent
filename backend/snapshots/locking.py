"""Reentrant thread/process catalog lock, independent of task workspace locks."""

import os
import threading
from contextlib import contextmanager
from functools import wraps

_registry_lock = threading.Lock()
_registry = {}


@contextmanager
def repository_lock(directory):
    key = str(directory)
    with _registry_lock:
        lock, state = _registry.setdefault(key, (threading.RLock(), threading.local()))
    with lock:
        if getattr(state, "held", False):
            yield
            return
        path = directory / "catalog.lock"
        if path.is_symlink():
            raise OSError("Snapshot lock must not be a link")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            state.held = True
            try:
                yield
            finally:
                state.held = False
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def locked(function):
    @wraps(function)
    def call(self, *args, **kwargs):
        with repository_lock(self.directory):
            return function(self, *args, **kwargs)
    return call
