"""Append-only journal. Metadata is a rebuildable, atomic projection."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from pathlib import Path

from backend.storage.files import atomic_json, internal_directory, sync_directory
from backend.storage.redaction import redact

from .metadata import now, project_meta
from .snapshot import REDUCER_VERSION, VERSION, digest, recovery_seed, runtime_view


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
        self.snapshot_path = self.directory / "journal_snapshot.json"
        self.view = []
        self._tail = []
        self._events_cache = None
        self._seed = []
        self.seq = 0
        self._offset = 0
        self._boundary = None
        self.recovery_info = {"source": "journal", "covered_seq": 0, "loaded_records": 0}
        self._restore_snapshot()
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

    def _restore_snapshot(self):
        if self.snapshot_path.is_symlink():
            raise ValueError("Journal snapshot must not be a link")
        if not self.snapshot_path.exists() or not self.path.exists():
            return
        try:
            wrapped = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
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
                or last != saved["boundary_event"]
            ):
                return
            self.view = saved["view"]
            self._seed = saved["recovery_seed"]
            if not isinstance(self.view, list) or not isinstance(self._seed, list):
                raise ValueError("Invalid snapshot projection")
            self.seq, self._offset, self._boundary = last["seq"], saved["offset"], boundary
            self.recovery_info.update(source="journal_snapshot", covered_seq=self.seq)
        except (KeyError, TypeError, ValueError, OSError):
            self.view, self._seed = [], []
            self.seq, self._offset, self._boundary = 0, 0, None
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

    def save_snapshot(self):
        with self._lock:
            if self.failed:
                raise OSError("Journal write outcome is uncertain; reopen before writing")
            if not self.seq:
                return
            stat = self.path.stat()
            saved = {
                "schema_version": VERSION,
                "reducer_version": REDUCER_VERSION,
                "session_id": self.session_id,
                "covered_seq": self.seq,
                "offset": self._offset,
                "boundary": self._boundary,
                "journal_identity": [stat.st_dev, stat.st_ino],
                "boundary_event": self.view[-1],
                "view": runtime_view(self.view),
                "recovery_seed": recovery_seed(self.recovery_view),
            }
            atomic_json(self.snapshot_path, {"data": saved, "digest": digest(saved)})

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
            if event_type in {"run_end", "context_compressed"}:
                try:
                    self.save_snapshot()
                except OSError:
                    pass  # Optional acceleration cache; the journal is already durable.
            return event

    def _redact(self, value):
        return redact(value, self.secrets)

    def rebuild_meta(self) -> dict:
        self.meta = self.project_meta(self.view, self.session_id)
        atomic_json(self.meta_path, self.meta)
        return self.meta

    project_meta = staticmethod(project_meta)

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
