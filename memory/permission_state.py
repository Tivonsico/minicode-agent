"""Conversation-local directory grants reconstructed from session events."""

import os
from pathlib import Path

from memory.project_assets import ProjectAssets
from tools.base import ToolError


def validate_directory(value: str | Path) -> Path:
    raw = Path(value)
    if not raw.is_absolute() or str(raw).startswith("\\\\"):
        raise ToolError("Enter an absolute local directory path.", "path")
    if ".." in raw.parts or any(":" in part for part in raw.parts[1:]):
        raise ToolError("Directory path contains traversal or an alternate stream.", "path")
    try:
        target = raw.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ToolError("Directory does not exist or cannot be resolved.", "path") from error
    if not target.is_dir() or target == Path(target.anchor):
        raise ToolError("Drive roots and non-directories cannot be authorized.", "path")
    protected = [Path(os.environ.get("WINDIR", "C:\\Windows"))]
    if os.name == "nt":
        protected += [Path(target.anchor) / name for name in ("Program Files", "Program Files (x86)", "ProgramData")]
    for item in protected:
        item = item.resolve()
        if target == item or target.is_relative_to(item) or item.is_relative_to(target):
            raise ToolError("System directories cannot be authorized.", "path")
    if any(part.lower() in {".minicode", ".git"} for part in target.parts):
        raise ToolError("Internal directories cannot be authorized.", "path")
    return target


class SessionPermissions:
    def __init__(self, root: Path, store):
        self.root = Path(root).resolve()
        self.assets = ProjectAssets(self.root)
        self.store = store
        self._extra: dict[str, Path] = {}
        self._active: str | None = None
        self.version = 0
        for event in store.events:
            kind = event.get("type")
            if kind == "directory_authorized":
                try:
                    path = Path(event["path"])
                    if not path.is_absolute():
                        continue
                except (KeyError, TypeError):
                    continue
                self._extra[str(path).casefold()] = path
                self._active = str(path).casefold()
                self.version = event["seq"]
            elif kind == "directory_revoked":
                self._extra.pop(str(event.get("path", "")).casefold(), None)
                if self._active == str(event.get("path", "")).casefold():
                    self._active = next(reversed(self._extra), None)
                self.version = event["seq"]
            elif kind == "directory_selected":
                key = str(event.get("path", "")).casefold()
                if key in self._extra:
                    self._active = key
                self.version = event["seq"]

    def _validate(self, value: str | Path) -> Path:
        path = validate_directory(value)
        if path.is_relative_to(self.root) or self.root.is_relative_to(path):
            raise ToolError("The minicode application directory and its ancestors cannot be work directories.", "path")
        return path

    def roots(self) -> tuple[Path, ...]:
        valid = []
        for path in self._extra.values():
            try:
                if self._validate(path) == path:
                    valid.append(path)
            except ToolError:
                continue
        active = self._extra.get(self._active)
        if active in valid:
            valid.remove(active)
            valid.insert(0, active)
        return tuple(valid)

    def active(self) -> Path | None:
        roots = self.roots()
        return roots[0] if roots else None

    def listing(self) -> list[dict]:
        items = []
        for path in self._extra.values():
            try:
                valid = self._validate(path) == path
                reason = "" if valid else "Directory target changed"
            except ToolError as error:
                valid, reason = False, str(error)
            items.append({"path": str(path), "fixed": False, "valid": valid, "reason": reason,
                          "active": valid and path == self.active()})
        return items

    def add(self, value: str) -> Path:
        path = self._validate(value)
        if str(path).casefold() in self._extra:
            raise ToolError("Directory is already authorized.", "path")
        self.assets.ensure_project(path)
        event = self.store.append("directory_authorized", path=str(path))
        self._extra[str(path).casefold()] = path
        self._active = str(path).casefold()
        self.version = event["seq"]
        return path

    def remove(self, value: str) -> Path:
        raw = Path(value)
        path = raw if str(raw).casefold() in self._extra else raw.resolve()
        key = str(path).casefold()
        if key not in self._extra:
            raise ToolError("Directory is not authorized.", "path")
        event = self.store.append("directory_revoked", path=str(self._extra[key]))
        removed = self._extra.pop(key)
        if self._active == key:
            self._active = next(reversed(self._extra), None)
        self.version = event["seq"]
        return removed

    def select(self, value: str) -> Path:
        key = str(Path(value).resolve()).casefold()
        if key not in self._extra or self._extra[key] not in self.roots():
            raise ToolError("Directory is not an active authorization.", "path")
        path = self._extra[key]
        self.assets.ensure_project(path)
        event = self.store.append("directory_selected", path=str(path))
        self._active, self.version = key, event["seq"]
        return path
