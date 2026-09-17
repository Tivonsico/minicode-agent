"""Path checks reduce accidental access; they are not an OS sandbox."""

import hashlib
import os
import re
import time
from pathlib import Path

from .base import ToolError

PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9 ]*PRIVATE KEY|OPENSSH PRIVATE KEY)-----")


def check_content(text: str):
    if PRIVATE_KEY.search(text):
        raise ToolError("Private key content is not available to file tools.", "sensitive")


def fingerprint(path: Path):
    data = path.read_bytes()
    stat = path.stat()
    return (hashlib.sha256(data).hexdigest(), stat.st_mtime_ns, stat.st_size)


class PathPolicy:
    def __init__(self, root, allowed_outputs=lambda: set(), extra_roots=lambda: (), *, include_root=True):
        self.root = Path(root).resolve()
        self.allowed_outputs = allowed_outputs
        self.extra_roots = extra_roots
        self.include_root = include_root

    def roots(self):
        return ((self.root,) if self.include_root else ()) + tuple(
            Path(path).resolve() for path in self.extra_roots()
        )

    def base(self):
        roots = self.roots()
        if not roots:
            raise ToolError("No work directory. Use /allow-dir <absolute path> first.", "path")
        return roots[0]

    def _containing_root(self, path, roots=None):
        matches = (root for root in (self.roots() if roots is None else roots) if path.is_relative_to(root))
        return max(matches, key=lambda root: len(root.parts), default=None)

    def display(self, path):
        path = Path(path)
        root = self._containing_root(path)
        return path.relative_to(root).as_posix() if root else str(path)

    def match_name(self, path, roots=None):
        path = Path(path)
        root = self._containing_root(path, roots)
        if root is None:
            raise ToolError("Path is outside authorized directories.", "path")
        return path.relative_to(root).as_posix()

    def resolve(self, value: str, *, output_read=False, roots=None):
        raw = Path(value)
        if ".." in raw.parts or any(":" in part for part in raw.parts[1:]):
            raise ToolError("Path traversal or alternate streams are forbidden.", "path")
        if not raw.is_absolute() and ":" in value:
            raise ToolError("Drive-relative paths and alternate streams are forbidden.", "path")
        allowed = {Path(p).resolve() for p in self.allowed_outputs()}
        saved_output = output_read and not raw.is_absolute() and (self.root / raw).resolve() in allowed
        candidate = raw if raw.is_absolute() else (self.root / raw if saved_output else self.base() / raw)
        target = candidate.resolve()
        roots = self.roots() if roots is None else roots
        internal_output = output_read and target in allowed and candidate == target
        if not self.include_root and (candidate.is_relative_to(self.root) or target.is_relative_to(self.root)) and not internal_output:
            raise ToolError("The minicode application directory is protected.", "path")
        if internal_output:
            return target
        if self._containing_root(candidate, roots) is None or self._containing_root(target, roots) is None:
            raise ToolError("Path is outside work directories; use /allow-dir <absolute path>.", "path")
        # Check both the lexical alias and resolved target for protected names.
        for path in (candidate, target):
            root = self._containing_root(path, roots)
            if root is None:
                raise ToolError("Path is outside authorized directories.", "path")
            parts = [part.lower() for part in path.relative_to(root).parts]
            if any(part in {".git", "project_system_prompt.md", "project_skills"}
                   or self._sensitive(part) for part in parts):
                raise ToolError("Sensitive files cannot be accessed.", "sensitive")
            if ".minicode" in parts:
                allowed = {Path(p).resolve() for p in self.allowed_outputs()}
                if not output_read or target not in allowed:
                    raise ToolError("Internal data is protected.", "path")
        if target.is_file() and target.stat().st_nlink > 1:
            raise ToolError("Files with hard-link aliases are protected.", "sensitive")
        return target

    @staticmethod
    def _sensitive(name):
        return (
            (name == ".env" or name.startswith(".env."))
            and name != ".env.example"
            or name
            in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "credentials.json"}
            or name.endswith((".pem", ".key", ".p12", ".pfx"))
        )

    def files(self, *, max_depth=None, include_dirs=False, stop=None, deadline=None, max_entries=10000):
        """Yield safe entries with a bounded walk; depth 1 means direct children only."""
        seen = set()
        visited = 0
        roots = self.roots()
        for root in roots:
            for directory, dirs, files in os.walk(root, followlinks=False):
                if stop is not None and stop.is_set():
                    return
                depth = len(Path(directory).relative_to(root).parts)
                kept = []
                for name in dirs:
                    visited += 1
                    if visited > max_entries or (deadline is not None and time.monotonic() > deadline):
                        raise ToolError("Search limit reached; use a narrower pattern.", "search_limit")
                    if stop is not None and stop.is_set():
                        return
                    path = Path(directory) / name
                    try:
                        resolved = self.resolve(str(path), roots=roots)
                        if not path.is_symlink() and not getattr(path, "is_junction", lambda: False)():
                            if include_dirs and resolved not in seen:
                                seen.add(resolved)
                                yield resolved
                            if max_depth is None or depth + 1 < max_depth:
                                kept.append(name)
                    except (ToolError, OSError):
                        pass
                dirs[:] = kept
                for name in files:
                    visited += 1
                    if visited > max_entries or (deadline is not None and time.monotonic() > deadline):
                        raise ToolError("Search limit reached; use a narrower pattern.", "search_limit")
                    if stop is not None and stop.is_set():
                        return
                    try:
                        path = self.resolve(str(Path(directory) / name), roots=roots)
                        if path.is_file() and path not in seen:
                            seen.add(path)
                            yield path
                    except (ToolError, OSError):
                        continue
