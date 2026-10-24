"""Large output references belong to one session, never a global namespace."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

from backend.conversation.store import SessionStore
from backend.storage.files import internal_directory, sync_directory


class OutputStore:
    def __init__(self, store: SessionStore, secrets=()):
        self.store = store
        self.root = store.root
        self.directory = internal_directory(store.root, "sessions", store.session_id, "outputs")
        self.secrets = tuple(value for value in secrets if value)

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return re.sub(
            r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
            "[REDACTED PRIVATE KEY]",
            text,
            flags=re.DOTALL,
        )

    def save(self, call_id: str, text: str, stream: str = "output") -> dict:
        text = self.redact(text)
        internal_directory(self.root, "sessions", self.store.session_id, "outputs")
        suffix = stream if stream in ("stdout", "stderr", "output") else "output"
        safe_id = hashlib.sha256(call_id.encode()).hexdigest()[:24]
        path = self.directory / f"{safe_id}-{uuid.uuid4().hex}.{suffix}.txt"
        with path.open("x", encoding="utf-8", newline="") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        sync_directory(self.directory)
        record = {
            "path": path.relative_to(self.root).as_posix(),
            "ref": path.relative_to(self.store.directory).as_posix(),
            "chars": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        self.store.append("output_saved", call_id=call_id, **record)
        return record

    def allowed_paths(self) -> set[Path]:
        result = set()
        for event in self.store.view:
            if event["type"] != "output_saved":
                continue
            ref = Path(event["ref"])
            if ref.is_absolute() or ".." in ref.parts:
                continue
            target = self.store.directory / ref
            if target.is_symlink() or not target.resolve().is_relative_to(self.directory.resolve()):
                continue
            result.add(target.resolve())
        return result

    def preview(self, call_id: str, text: str, stream: str = "output", limit: int = 16000) -> str:
        text = self.redact(text)
        if len(text) <= limit:
            return text
        record = self.save(call_id, text, stream)
        note = f"\n[输出已截断；完整内容 {record['chars']} 字符，ReadFile: {record['path']}]\n"
        remaining = max(0, limit - len(note))
        if not remaining:
            return note[: max(0, limit)]
        return text[: remaining // 2] + note + text[-(remaining - remaining // 2) :]
