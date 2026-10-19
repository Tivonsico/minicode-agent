"""User-operated directory discovery, separate from agent filesystem grants."""

import os
from pathlib import Path

from backend.tools.base import ToolError


class DirectoryBrowser:
    def __init__(self, permissions):
        self.permissions = permissions

    def _path(self, value):
        raw = Path(value)
        if not raw.is_absolute() or ".." in raw.parts or str(raw).startswith("\\\\"):
            raise ToolError("请选择本机文件夹。", "path")
        current = Path(raw.anchor)
        for part in raw.parts[1:]:
            current /= part
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                raise ToolError("无法浏览链接文件夹。", "path")
        if any(part.startswith(".") for part in raw.parts[1:]):
            raise ToolError("隐藏文件夹不可浏览。", "path")
        if not raw.is_dir():
            raise ToolError("文件夹不存在或不可访问。", "path")
        return raw.resolve()

    def browse(self, value=None):
        path = self._path(value or self.permissions.active() or Path.home())
        try:
            self.permissions._validate(path)
            selectable, reason = True, ""
        except ToolError:
            selectable, reason = False, "进入项目文件夹后再选择；系统目录与应用目录不能授权。"
        # System trees may be shown as navigation entries, but never read.
        blocked = {"System", "Library", "usr", "bin", "sbin", "etc", "dev", "proc", "sys"}
        if os.name != "nt" and path.parts[1:2] and path.parts[1] in blocked:
            raise ToolError("系统文件夹不可浏览。", "path")
        entries = []
        truncated = False
        try:
            with os.scandir(path) as iterator:
                for index, item in enumerate(iterator):
                    if index >= 20000:
                        truncated = True
                        break
                    if item.name.startswith(".") or item.is_symlink():
                        continue
                    if not item.is_dir(follow_symlinks=False):
                        continue
                    child = Path(item.path)
                    if getattr(child, "is_junction", lambda: False)():
                        continue
                    if child.is_relative_to(self.permissions.root):
                        continue
                    if os.name != "nt" and path == Path("/") and item.name in blocked:
                        continue
                    entries.append({"name": item.name, "path": str(child)})
        except OSError as error:
            raise ToolError("无法读取这个文件夹，请选择其他位置。", "path") from error
        entries.sort(key=lambda item: item["name"].casefold())
        shortcuts = []
        home = Path.home()
        candidates = [
            ("个人文件夹", home),
            ("桌面", home / "Desktop"),
            ("文稿", home / "Documents"),
            ("项目", self.permissions.root.parent),
        ]
        if os.name == "nt":
            candidates += [
                (f"{letter} 盘", Path(f"{letter}:/")) for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            ]
        else:
            candidates += [("本机", Path("/"))]
        for name, shortcut in candidates:
            if shortcut.is_dir() and not shortcut.is_symlink():
                shortcuts.append({"name": name, "path": str(shortcut)})
        return {
            "path": str(path),
            "parent": str(path.parent) if path.parent != path else None,
            "entries": entries[:1000],
            "shortcuts": shortcuts,
            "selectable": selectable,
            "reason": reason,
            "truncated": truncated or len(entries) > 1000,
        }
