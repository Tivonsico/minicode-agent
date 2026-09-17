"""Append-only journal. Metadata is a rebuildable, atomic projection."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def internal_directory(root: Path, *parts: str) -> Path:
    root = root.resolve()
    current = root
    for part in (".minicode", *parts):
        if part in ("", ".", "..") or "/" in part or "\\" in part:
            raise ValueError("Invalid internal path")
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Internal data directory must not be a link")
        current.mkdir(exist_ok=True)
        if not current.resolve().is_relative_to(root):
            raise ValueError("Internal data escaped project")
    return current


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
                break
            except PermissionError as exc:
                # Windows indexers/antivirus may briefly hold a sharing lock.
                if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 3:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


class SessionStore:
    def __init__(self, root: Path, session_id: str | None = None, secrets=()):
        self.root = root.resolve()
        self.secrets = tuple(value for value in secrets if value)
        self.session_id = session_id or uuid.uuid4().hex
        if not self.session_id.isalnum():
            raise ValueError("Invalid session ID")
        self.directory = internal_directory(self.root, "sessions", self.session_id)
        self.path = self.directory / "session.jsonl"
        self.meta_path = self.directory / "session.meta"
        if self.path.is_symlink() or self.meta_path.is_symlink():
            raise ValueError("Session files must not be links")
        self._lock = threading.RLock()
        self.events: list[dict] = []
        self._read()
        self.rebuild_meta()

    def _read(self) -> None:
        if not self.path.exists():
            return
        raw = self.path.read_bytes()
        lines = raw.splitlines(keepends=True)
        offset = 0
        for index, line in enumerate(lines):
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("seq") != index + 1:
                    raise ValueError("Invalid sequence")
                if event.get("session_id") != self.session_id:
                    raise ValueError("Wrong session ID")
            except (ValueError, UnicodeDecodeError) as exc:
                if index != len(lines) - 1 or line.endswith(b"\n"):
                    raise ValueError(f"Corrupt journal at record {index + 1}") from exc
                # Preserve the damaged bytes, then repair only the incomplete tail.
                diagnostic = self.directory / ("incomplete-" + uuid.uuid4().hex + ".bin")
                diagnostic.write_bytes(line)
                with self.path.open("r+b") as file:
                    file.truncate(offset)
                break
            self.events.append(event)
            offset += len(line)
        if self.events and self.path.stat().st_size and not self.path.read_bytes().endswith(b"\n"):
            with self.path.open("ab") as file:
                file.write(b"\n")

    def append(self, event_type: str, **data) -> dict:
        with self._lock:
            internal_directory(self.root, "sessions", self.session_id)
            if self.path.is_symlink():
                raise ValueError("Journal must not be a link")
            event = self._redact(dict(data))
            event.update(
                type=event_type, seq=len(self.events) + 1, session_id=self.session_id, time=now()
            )
            encoded = json.dumps(event, ensure_ascii=False) + "\n"
            with self.path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            self.events.append(event)
            if event_type not in ("assistant_delta", "tool_delta"):
                self.rebuild_meta()
            return event

    def _redact(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def rebuild_meta(self) -> dict:
        messages = {
            e["message_id"]: e for e in self.events if e["type"] == "message" and "message_id" in e
        }
        usage = {
            e["request_id"]: e for e in self.events if e["type"] == "usage" and "request_id" in e
        }
        by_purpose: dict[str, dict] = {}
        total = 0
        unknown = 0
        for event in usage.values():
            bucket = by_purpose.setdefault(
                event.get("purpose", "main"), {"tokens": 0, "unknown": 0}
            )
            record = event.get("usage")
            if record is None or record.get("total_tokens") is None:
                unknown += 1
                bucket["unknown"] += 1
            else:
                amount = record["total_tokens"]
                total += amount
                bucket["tokens"] += amount
        users = [e for e in messages.values() if e.get("role") == "user"]
        endings = [e for e in self.events if e["type"] == "turn_end"]
        starts = [e for e in self.events if e["type"] == "turn_start"]
        active = bool(starts and (not endings or starts[-1]["seq"] > endings[-1]["seq"]))
        final_messages = [e for e in messages.values() if e.get("is_final")]
        self.meta = {
            "session_id": self.session_id,
            "title": users[0].get("content", "")[:60] if users else "新会话",
            "summary": final_messages[-1].get("content", "")[:200] if final_messages else "",
            "message_count": len(messages),
            "token_total": total,
            "usage_incomplete": bool(unknown),
            "unknown_requests": unknown,
            "usage_by_purpose": by_purpose,
            "created_at": self.events[0]["time"] if self.events else now(),
            "last_active_at": self.events[-1]["time"] if self.events else now(),
            "status": "active" if active else "idle",
            "last_termination": endings[-1].get("reason") if endings else None,
        }
        atomic_json(self.meta_path, self.meta)
        return self.meta

    @classmethod
    def list_sessions(cls, root: Path) -> list[dict]:
        parent = internal_directory(root, "sessions")
        result = []
        for path in parent.iterdir():
            if not path.is_dir() or not path.name.isalnum() or path.is_symlink():
                continue
            try:
                result.append(cls(root, path.name).meta)
            except (ValueError, OSError) as exc:
                result.append(
                    {
                        "session_id": path.name,
                        "title": "损坏的会话",
                        "status": "error",
                        "last_active_at": "",
                        "error": type(exc).__name__,
                    }
                )
        return sorted(result, key=lambda e: e["last_active_at"], reverse=True)
