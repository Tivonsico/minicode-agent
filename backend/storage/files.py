"""Durable file primitives; independent of session journals and storage backends."""

import json
import os
import time
import uuid
from pathlib import Path


def internal_directory(root: Path, *parts: str) -> Path:
    root = root.resolve()
    current = root
    for part in (".minicode", *parts):
        if part in ("", ".", "..") or "/" in part or "\\" in part:
            raise ValueError("Invalid internal path")
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Internal data directory must not be a link")
        created = not current.exists()
        current.mkdir(exist_ok=True)
        if created:
            sync_directory(current.parent)
        if not current.resolve().is_relative_to(root):
            raise ValueError("Internal data escaped project")
    return current


def sync_directory(path: Path) -> None:
    """POSIX directory entries need their own sync; Windows requires platform guarantees."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, data: object) -> None:
    if path.is_symlink():
        raise ValueError("Internal file must not be a link")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                sync_directory(path.parent)
                break
            except PermissionError as exc:
                # Windows indexers/antivirus may briefly hold a sharing lock.
                if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 3:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)
