"""Append-only journal. Metadata is a rebuildable, atomic projection."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .snapshot import REDUCER_VERSION, VERSION, digest, recovery_seed, runtime_view


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
        self.failed = False
        self.checkpoint_path = self.directory / "checkpoint.json"
        self.view = []
        self._tail = []
        self._events_cache = None
        self._seed = []
        self.seq = 0
        self._offset = 0
        self._boundary = None
        self.restored_checkpoint = None
        self.recovery_info = {"source": "journal", "covered_seq": 0, "loaded_records": 0}
        self._restore_checkpoint()
        self._read()
        self.rebuild_meta()

    @property
    def events(self):
        """Full audit history is loaded only when explicitly requested."""
        if self._events_cache is None:
            self._events_cache = self._scan(0, 1, repair=False)[0]
        return self._events_cache

    @property
    def recovery_view(self):
        return sorted(
            {e["seq"]: e for e in self._seed + self._tail}.values(), key=lambda e: e["seq"]
        )

    def events_since(self, seq):
        return [e for e in self._tail if e["seq"] > seq]

    def _restore_checkpoint(self):
        if self.checkpoint_path.is_symlink():
            raise ValueError("Checkpoint must not be a link")
        if not self.checkpoint_path.exists() or not self.path.exists():
            return
        try:
            wrapped = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            saved = wrapped["data"]
            if wrapped["digest"] != digest(saved):
                return
            stat = self.path.stat()
            if (
                saved["schema_version"] != VERSION
                or saved["reducer_version"] != REDUCER_VERSION
                or saved["session_id"] != self.session_id
                or saved["journal_identity"] != [stat.st_dev, stat.st_ino]
                or not 0 < saved["offset"] <= stat.st_size
            ):
                return
            boundary = saved["boundary"]
            if (
                boundary["start"] < 0
                or boundary["size"] <= 0
                or boundary["start"] + boundary["size"] != saved["offset"]
            ):
                return
            with self.path.open("rb") as file:
                file.seek(boundary["start"])
                line = file.read(boundary["size"])
            last = json.loads(line)
            if (
                hashlib.sha256(line).hexdigest() != boundary["digest"]
                or last["seq"] != saved["covered_seq"]
                or last.get("session_id") != self.session_id
                or last["type"] != "checkpoint"
                or last != saved["checkpoint"]
            ):
                return
            if not isinstance(last.get("state"), dict):
                return
            self.restored_checkpoint = last
            self.view = saved["view"]
            self._seed = saved["recovery_seed"]
            if not isinstance(self.view, list) or not isinstance(self._seed, list):
                raise ValueError("Invalid snapshot projection")
            self.seq, self._offset, self._boundary = last["seq"], saved["offset"], boundary
            self.recovery_info.update(source="checkpoint", covered_seq=self.seq)
        except (KeyError, TypeError, ValueError, OSError):
            self.view, self._seed = [], []
            self.seq, self._offset, self._boundary = 0, 0, None
            self.restored_checkpoint = None
            self.recovery_info.update(source="journal", covered_seq=0)

    def _scan(self, offset, expected, *, repair):
        records = []
        boundary = None
        if not self.path.exists():
            return records, offset, boundary
        with self.path.open("rb") as file:
            file.seek(offset)
            while line := file.readline():
                begin = offset
                offset += len(line)
                try:
                    event = json.loads(line)
                    if (
                        not isinstance(event, dict)
                        or event.get("seq") != expected
                        or event.get("session_id") != self.session_id
                    ):
                        raise ValueError("Invalid event identity or sequence")
                except (ValueError, UnicodeDecodeError) as exc:
                    if not repair or line.endswith(b"\n") or file.read(1):
                        raise ValueError(f"Corrupt journal at record {expected}") from exc
                    diagnostic = self.directory / ("incomplete-" + uuid.uuid4().hex + ".bin")
                    with diagnostic.open("xb") as target:
                        target.write(line)
                        target.flush()
                        os.fsync(target.fileno())
                    with self.path.open("r+b") as target:
                        target.truncate(begin)
                        target.flush()
                        os.fsync(target.fileno())
                    sync_directory(self.directory)
                    offset = begin
                    break
                if not line.endswith(b"\n") and repair:
                    with self.path.open("ab") as target:
                        target.write(b"\n")
                        target.flush()
                        os.fsync(target.fileno())
                    line += b"\n"
                    offset += 1
                boundary = {
                    "start": begin,
                    "size": len(line),
                    "digest": hashlib.sha256(line).hexdigest(),
                }
                records.append(event)
                expected += 1
        return records, offset, boundary

    def _read(self):
        records, offset, boundary = self._scan(self._offset, self.seq + 1, repair=True)
        self._tail = records
        self.view.extend(records)
        self._offset = offset
        if records:
            self.seq, self._boundary = records[-1]["seq"], boundary
        self.recovery_info["loaded_records"] = len(records)

    def save_checkpoint(self, record):
        with self._lock:
            if self.failed:
                raise OSError("Journal write outcome is uncertain; reopen before writing")
            stat = self.path.stat()
            saved = {
                "schema_version": VERSION,
                "reducer_version": REDUCER_VERSION,
                "session_id": self.session_id,
                "covered_seq": self.seq,
                "offset": self._offset,
                "boundary": self._boundary,
                "journal_identity": [stat.st_dev, stat.st_ino],
                "checkpoint": record,
                "view": runtime_view(self.view),
                "recovery_seed": recovery_seed(self.recovery_view),
            }
            atomic_json(self.checkpoint_path, {"data": saved, "digest": digest(saved)})

    def append(self, event_type: str, **data) -> dict:
        with self._lock:
            if self.failed:
                raise OSError("Journal write outcome is uncertain; reopen before writing")
            internal_directory(self.root, "sessions", self.session_id)
            if self.path.is_symlink():
                raise ValueError("Journal must not be a link")
            from backend.observability.scope import current

            event = self._redact({"schema_version": 1, **current(self), **data})
            event.update(type=event_type, seq=self.seq + 1, session_id=self.session_id, time=now())
            encoded = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            try:
                existed = self.path.exists()
                with self.path.open("ab") as file:
                    if file.tell() != self._offset:
                        raise OSError("Journal changed outside this writer; reopen")
                    file.write(encoded)
                    file.flush()
                    os.fsync(file.fileno())
                if not existed:
                    sync_directory(self.directory)
                self._boundary = {
                    "start": self._offset,
                    "size": len(encoded),
                    "digest": hashlib.sha256(encoded).hexdigest(),
                }
                self._offset += len(encoded)
                self.seq = event["seq"]
                self.view.append(event)
                self._tail.append(event)
                if self._events_cache is not None:
                    self._events_cache.append(event)
                if event_type not in ("assistant_delta", "tool_delta"):
                    self.rebuild_meta()
            except (OSError, ValueError):
                self.failed = True
                raise
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
        self.meta = self.project_meta(self.view, self.session_id)
        atomic_json(self.meta_path, self.meta)
        return self.meta

    @staticmethod
    def project_meta(view, session_id) -> dict:
        messages = {}
        for e in view:
            if e["type"] == "run_accepted" and isinstance(e.get("input"), dict):
                messages[e["input"]["message_id"]] = {**e, **e["input"]}
            elif e["type"] == "message" and "message_id" in e:
                messages[e["message_id"]] = e
        usage = {
            e["request_id"]: e for e in view if e["type"] == "usage" and "request_id" in e
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
        endings = [e for e in view if e["type"] == "turn_end"]
        starts = [e for e in view if e["type"] == "turn_start"]
        active = bool(starts and (not endings or starts[-1]["seq"] > endings[-1]["seq"]))
        final_messages = [e for e in messages.values() if e.get("is_final")]
        return {
            "session_id": session_id,
            "title": users[0].get("content", "")[:60] if users else "新会话",
            "summary": final_messages[-1].get("content", "")[:200] if final_messages else "",
            "message_count": len(messages),
            "token_total": total,
            "usage_incomplete": bool(unknown),
            "unknown_requests": unknown,
            "usage_by_purpose": by_purpose,
            "created_at": view[0]["time"] if view else now(),
            "last_active_at": view[-1]["time"] if view else now(),
            "status": "active" if active else "idle",
            "last_termination": endings[-1].get("reason") if endings else None,
        }

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
