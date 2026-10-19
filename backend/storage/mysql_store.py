"""MySQL event store using the same runtime projections as the local journal."""
from __future__ import annotations

import copy
import json
import threading
import uuid
from pathlib import Path

from backend.checkpoint.manager import empty_state, reduce_state
from backend.conversation.snapshot import (
    REDUCER_VERSION,
    VERSION,
    digest,
    identity,
    recovery_seed,
    runtime_view,
)
from backend.conversation.store import SessionStore, internal_directory, now
from backend.observability.scope import current

from .mysql import encoded
from .nonblocking import io_method


def operation_id(session_id, event):
    return digest([session_id, identity(event, "call_id")])


class MySQLSessionStore:
    _redact = SessionStore._redact

    def __init__(self, database, root, session_id=None, secrets=()):
        self.database = database
        self.root = Path(root).resolve()
        self.session_id = session_id or uuid.uuid4().hex
        if not self.session_id.isascii() or not self.session_id.isalnum() or len(self.session_id) > 128:
            raise ValueError("Invalid session ID")
        self.secrets = tuple(s for s in secrets if s)
        self.directory = internal_directory(self.root, "sessions", self.session_id)
        self.path = self.directory / "session.jsonl"  # Only used for attachment path policy.
        self._lock = threading.RLock()
        self.failed = False
        self.owner = None
        self.claim = None
        self._events_cache = None
        self._tail, self._seed, self.view = [], [], []
        self.restored_checkpoint = None
        self.seq = 0
        self.recovery_info = {"source": "mysql", "covered_seq": 0, "loaded_records": 0}
        if session_id is None:
            meta = SessionStore.project_meta([], self.session_id)
            database.transaction(lambda c: c.execute(
                "INSERT INTO mc_sessions(id,meta,state) VALUES (%s,%s,%s)",
                (self.session_id, encoded(meta), encoded(empty_state()))))
        self._load()

    @io_method
    def _load(self):
        # Consistent read: checkpoint, suffix and session projection share one read view.
        with self.database.connection() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT seq,meta,state FROM mc_sessions WHERE id=%s",
                                   (self.session_id,))
                    row = cursor.fetchone()
                    if row is None:
                        raise KeyError("会话不存在")
                    self.meta, self.state = json.loads(row["meta"]), json.loads(row["state"])
                    cursor.execute("SELECT body FROM mc_checkpoints WHERE session_id=%s",
                                   (self.session_id,))
                    checkpoint = cursor.fetchone()
                    covered = 0
                    if checkpoint:
                        try:
                            wrapper = json.loads(checkpoint["body"])
                            saved = wrapper["data"]
                            covered = saved["covered_seq"]
                            cursor.execute("SELECT body FROM mc_events WHERE session_id=%s AND seq=%s",
                                           (self.session_id, covered))
                            boundary = cursor.fetchone()
                            if (wrapper["digest"] != digest(saved)
                                or saved["schema_version"] != VERSION
                                or saved["reducer_version"] != REDUCER_VERSION
                                or saved["session_id"] != self.session_id
                                or not 0 < covered <= row["seq"]
                                or not boundary or json.loads(boundary["body"]) != saved["checkpoint"]
                                or saved["checkpoint"]["type"] != "checkpoint"
                                or not isinstance(saved["view"], list)
                                or not isinstance(saved["recovery_seed"], list)):
                                raise ValueError("Invalid checkpoint")
                            self.view = saved["view"]
                            self._seed = saved["recovery_seed"]
                            self.restored_checkpoint = saved["checkpoint"]
                            self.recovery_info.update(source="checkpoint", covered_seq=covered)
                        except (ValueError, KeyError, TypeError):
                            covered = 0
                    cursor.execute("SELECT body FROM mc_events WHERE session_id=%s AND seq>%s ORDER BY seq",
                                   (self.session_id, covered))
                    self._tail = [json.loads(e["body"]) for e in cursor.fetchall()]
                    if [e["seq"] for e in self._tail] != list(range(covered + 1, row["seq"] + 1)):
                        raise ValueError("Corrupt MySQL event sequence")
                    self.view.extend(self._tail)
                    self.seq = row["seq"]
                    self.recovery_info["loaded_records"] = len(self._tail)
            finally:
                connection.rollback()

    @property
    @io_method
    def events(self):
        return [json.loads(e["body"]) for e in self.database.query(
            "SELECT body FROM mc_events WHERE session_id=%s ORDER BY seq", (self.session_id,))]

    @property
    def recovery_view(self):
        return sorted({e["seq"]: e for e in self._seed + self._tail}.values(), key=lambda e: e["seq"])

    def events_since(self, seq):
        return [e for e in self._tail if e["seq"] > seq]

    def find_event(self, event_id):
        rows = self.database.query("SELECT body FROM mc_events WHERE event_id=%s AND session_id=%s",
                                   (event_id, self.session_id))
        return json.loads(rows[0]["body"]) if rows else None

    def _row(self, cursor):
        cursor.execute("SELECT seq,owner FROM mc_sessions WHERE id=%s FOR UPDATE", (self.session_id,))
        row = cursor.fetchone()
        if not row or row["seq"] != self.seq or row["owner"] != self.owner:
            raise ValueError("会话写入权或版本已变化，请刷新；不能并发恢复活跃会话")
        return row

    @io_method
    def append(self, event_type, **data):
        with self._lock:
            if self.failed:
                raise OSError("MySQL writer is frozen; reopen and verify")
            event_id = uuid.uuid4().hex
            event = self._redact({"schema_version": 1, **current(self), **data})
            event.update(type=event_type, event_id=event_id, seq=self.seq + 1,
                         session_id=self.session_id, time=now())
            if event_type in {"tool_start", "operation_prepared", "tool_running", "tool_result", "operation_reconciled"}:
                event["operation_id"] = operation_id(self.session_id, event)
            if event_type == "operation_prepared":
                intent = next(e for e in reversed(self.view) if e["type"] == "tool_start"
                              and identity(e, "call_id") == identity(event, "call_id"))
                event.update(input=intent["input"], intent_seq=intent["seq"])
            state = reduce_state(copy.deepcopy(self.state), event)
            meta = SessionStore.project_meta(self.view + [event], self.session_id)
            def write(cursor):
                self._row(cursor)
                self._operation(cursor, event)
                cursor.execute("INSERT INTO mc_events VALUES (%s,%s,%s,%s)",
                               (self.session_id, event["seq"], event_id, encoded(event)))
                cursor.execute("UPDATE mc_sessions SET seq=%s,meta=%s,state=%s WHERE id=%s",
                               (event["seq"], encoded(meta), encoded(state), self.session_id))
                if event_type == "checkpoint":
                    saved = {"schema_version": VERSION, "reducer_version": REDUCER_VERSION,
                             "session_id": self.session_id, "covered_seq": event["seq"],
                             "checkpoint": event, "view": runtime_view(self.view + [event]),
                             "recovery_seed": recovery_seed(self.recovery_view + [event])}
                    cursor.execute("INSERT INTO mc_checkpoints VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE covered_seq=VALUES(covered_seq),body=VALUES(body)",
                                   (self.session_id, event["seq"], encoded({"data": saved, "digest": digest(saved)})))
                return event
            try:
                event = self.database.transaction(write, command_id=event_id)
            except (OSError, ValueError):
                self.failed = True
                raise
            self.seq, self.meta, self.state = event["seq"], meta, state
            self.view.append(event)
            self._tail.append(event)
            return event

    def _operation(self, cursor, event):
        kind = event["type"]
        if kind not in {"operation_prepared", "tool_running", "tool_result", "operation_reconciled"}:
            return
        oid = operation_id(self.session_id, event)
        cursor.execute("SELECT status,result_seq FROM mc_operations WHERE id=%s FOR UPDATE", (oid,))
        previous = cursor.fetchone()
        if kind == "tool_running":
            if not previous or previous["status"] != "PREPARED" or not self.claim:
                raise ValueError("工具未准备或未取得工作区执行权")
            cursor.execute("SELECT owner FROM mc_claims WHERE id=%s", (self.claim,))
            claim = cursor.fetchone()
            if not claim or claim["owner"] != self.owner:
                raise ValueError("工作区执行权已变化")
            status = "STARTED"
        elif kind == "operation_prepared":
            if previous:
                raise ValueError("操作已准备或执行，禁止重复派发")
            status = "PREPARED"
        elif kind == "operation_reconciled":
            if (not previous or previous["status"] != "UNKNOWN"
                or previous["result_seq"] != event.get("result_seq")
                or not event.get("processes_stopped") or not event.get("evidence", "").strip()):
                raise ValueError("核实依据不足或操作已经变化")
            status = "RECONCILED"
        else:
            if previous and previous["status"] not in {"PREPARED", "STARTED"}:
                raise ValueError("操作已有结果，禁止重复保存")
            status = event.get("status", "unknown").upper()
        cursor.execute("INSERT INTO mc_operations VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE status=VALUES(status),result_seq=VALUES(result_seq),body=VALUES(body)",
                       (oid, self.session_id, status,
                        event.get("result_seq") if kind == "operation_reconciled" else event["seq"],
                        encoded(event)))

    @io_method
    def save_checkpoint(self, record):
        # Already atomically committed with the checkpoint event and state.
        if self.failed or record["seq"] != self.seq:
            raise OSError("Invalid checkpoint boundary")

    @io_method
    def acquire_owner(self):
        token = uuid.uuid4().hex
        def claim(cursor):
            cursor.execute("SELECT owner FROM mc_sessions WHERE id=%s FOR UPDATE", (self.session_id,))
            if cursor.fetchone()["owner"] is not None:
                raise ValueError("会话仍被执行者占用；确认旧执行者及子进程停止后，填写证据解除占用")
            cursor.execute("UPDATE mc_sessions SET owner=%s WHERE id=%s", (token, self.session_id))
            return token
        self.owner = self.database.transaction(claim)
        # Reads before claiming may be stale. Reload only after exclusive ownership.
        self._tail, self._seed, self.view = [], [], []
        self.restored_checkpoint = None
        self.recovery_info = {"source": "mysql", "covered_seq": 0, "loaded_records": 0}
        self._load()
        self.failed = False

    @io_method
    def close(self):
        if self.owner and not self.failed:
            def release(cursor):
                self._row(cursor)
                cursor.execute("UPDATE mc_sessions SET owner=NULL WHERE id=%s", (self.session_id,))
            self.database.transaction(release)
            self.owner = None
