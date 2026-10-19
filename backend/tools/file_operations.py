import asyncio
import os
import tempfile

from backend.conversation.store import sync_directory
from backend.permissions.paths import check_content, fingerprint

from .base import ToolError


class FileOperations:
    def _read(self, path):
        data = path.read_bytes()
        text = data.decode("utf-8-sig")
        if "\x00" in text:
            raise ToolError("Binary file is not supported.", "binary")
        check_content(text)
        return text

    async def _in_worker(self, function, *args):
        # A cancelled asyncio task does not stop a running filesystem thread.
        # Join it before releasing the read group, so no read overlaps a later write.
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            await asyncio.gather(worker, return_exceptions=True)
            raise

    def _snapshot(self, path):
        before = fingerprint(path)
        text = self._read(path)
        if fingerprint(path) != before:
            raise ToolError("File changed during reading; retry.", "stale_read")
        return before, text

    def _read_range(self, path, start, count):
        before, text = self._snapshot(path)
        lines = text.splitlines()
        content = f"{self.policy.display(path)} | total_lines={len(lines)} | sha256={before[0]}\n"
        content += "\n".join(
            f"{i + 1}: {line}"
            for i, line in enumerate(lines[start - 1 : start - 1 + count], start - 1)
        )
        return before, content

    def _grep_matches(self, path, pattern, relative):
        _, text = self._snapshot(path)
        return [
            f"{relative}:{i}: {line}"
            for i, line in enumerate(text.splitlines(), 1)
            if pattern in line
        ]

    def _verify_read(self, path):
        if path not in self.reads or fingerprint(path) != self.reads[path]:
            raise ToolError(
                "File must be read again before modification; no current matching read exists.",
                "stale_read",
            )

    def _write(self, raw_path, path, content, existed):
        check_content(content)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Resolve again immediately before creating a sibling temporary file.
        if self.policy.resolve(raw_path) != path:
            raise ToolError("Path changed.", "path")
        if existed:
            self._verify_read(path)
        elif path.exists():
            raise ToolError("File appeared; read before overwriting.", "stale_read")
        fd, temporary = tempfile.mkstemp(prefix=".minicode-edit-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if self.policy.resolve(raw_path) != path:
                raise ToolError("Path changed.", "path")
            if existed:
                self._verify_read(path)
                os.replace(temporary, path)
            else:
                # Hard link creation fails if another process creates the destination.
                os.link(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        sync_directory(path.parent)
        self.reads.pop(path, None)
