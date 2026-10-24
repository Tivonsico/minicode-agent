import asyncio
import hashlib
import os
import tempfile

from backend.observability.scope import current
from backend.permissions.paths import check_content, fingerprint
from backend.storage.files import sync_directory

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
        return before, content, len(lines)

    def record_text_write(self, cid, path, content, *, executable=False):
        # Readback proves a plain-text edit, not that executable code works.
        name = path.name.casefold()
        if (path.suffix.lower() != ".txt" or executable or content.startswith("#!")
                or name in {"cmakelists.txt", "cmakecache.txt", "meson_options.txt"}
                or name.startswith(("requirements", "constraints"))):
            self.text_writes.pop(path, None)
            return
        previous = self.text_writes.get(path, {})
        source = current(getattr(self.output_store, "store", None))
        self.text_writes[path] = {
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "ranges": [],
            "read_version": None,
            "mutations": previous.get("mutations", []) + [
                {"call_id": cid, "request_id": source.get("request_id")}
            ],
        }

    def verify_text_read(self, path, before, start, count, total):
        expected = self.text_writes.get(path)
        if not expected:
            return
        if expected["read_version"] != before:
            expected["ranges"] = []
            expected["read_version"] = before
        if before[0] != expected["sha256"]:
            return
        expected["ranges"].append((start, min(start + count - 1, total)))
        covered = 0
        merged = []
        for low, high in sorted(expected["ranges"]):
            if merged and low <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], high))
            else:
                merged.append((low, high))
        expected["ranges"] = merged
        if merged and merged[0][0] == 1:
            covered = merged[0][1]
        if covered >= total and merged[0][0] == 1:
            self.emit(
                "file_verification_result", status="passed", source="readback",
                path=str(path), sha256=before[0], mutations=expected["mutations"],
            )

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
