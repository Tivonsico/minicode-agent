"""Descriptor-relative workspace access rejects symlinks in every component."""

import os
import stat
import uuid
from contextlib import contextmanager

from backend.storage.files import sync_directory


@contextmanager
def parent_directory(project, path, *, create=False):
    relative = path.relative_to(project)
    if ".." in relative.parts:
        raise ValueError("Invalid snapshot path")
    if os.open not in os.supports_dir_fd:
        # Windows uses revalidation around each operation, as do existing file
        # tools. POSIX additionally anchors all operations to directory handles.
        for item in [path, *path.parents]:
            if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
                raise OSError("文件路径包含链接，不能保存或恢复")
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        yield None
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(project.anchor, flags)
    try:
        parts = project.parts[1:] + relative.parts[:-1]
        for index, part in enumerate(parts):
            if create and index >= len(project.parts) - 1:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            following = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = following
        yield fd
    finally:
        os.close(fd)


def read_regular(project, path, limit):
    with parent_directory(project, path) as parent:
        name = path.name if parent is not None else path
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink > 1:
                raise OSError("文件类型或链接数量发生变化")
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        def version(info):
            return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size, info.st_mode
        if version(before) != version(after) or version(before) != version(current):
            raise OSError("文件在读取过程中发生变化")
        return data, before


def replace_file(project, path, data, mode, expected, matches):
    with parent_directory(project, path, create=data is not None) as parent:
        if not matches():
            raise ValueError("恢复中检测到文件变化，已停止；恢复前副本已固定保留")
        name = path.name if parent is not None else path
        if data is None:
            os.unlink(name, dir_fd=parent)
        else:
            temporary = ".minicode-restore-" + uuid.uuid4().hex
            temporary_path = temporary if parent is not None else path.parent / temporary
            fd = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary_path, mode, dir_fd=parent)
                if not matches():
                    raise ValueError("恢复中检测到文件变化，已停止；恢复前副本已固定保留")
                # Also prove the directory descriptor still names the current
                # authorized parent before any replacement, not only its contents.
                if parent is not None:
                    info, current = os.fstat(parent), path.parent.stat()
                    if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                        raise ValueError("恢复中目录路径发生变化，请重新预览")
                if expected is None:
                    os.link(temporary_path, name, src_dir_fd=parent, dst_dir_fd=parent)
                else:
                    os.replace(temporary_path, name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary_path, dir_fd=parent)
                except FileNotFoundError:
                    pass
        if parent is not None:
            os.fsync(parent)
        else:
            sync_directory(path.parent)
