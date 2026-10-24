"""Session-local business identities and durable outcomes for opt-in tools.

The caller supplies an approved intent and a server-selected action slot. Neither
is a provider call ID. UNKNOWN is committed before any external dispatch. A
successful local record cannot make external commit + local persistence atomic.
"""
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


class OperationConflict(ValueError):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class OperationJournal:
    def __init__(self, directory, session_id):
        self.session_id = session_id
        self.path = Path(directory) / "business-operations.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Operation journal must not be a link")
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS operations ("
                       "operation_id TEXT PRIMARY KEY, intent TEXT NOT NULL, slot TEXT NOT NULL, "
                       "tool TEXT NOT NULL, payload TEXT NOT NULL, fingerprint TEXT NOT NULL, "
                       "state TEXT NOT NULL, result TEXT, evidence TEXT, reconciled_seq INTEGER DEFAULT 0, "
                       "UNIQUE(intent,slot))")
            db.execute("CREATE TABLE IF NOT EXISTS bindings ("
                       "call_identity TEXT PRIMARY KEY, operation_id TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def prepare(self, intent, slot, tool, arguments):
        if not all(isinstance(s, str) and s.strip() for s in (intent, slot, tool)):
            raise ValueError("Approved intent, action slot and tool are required")
        payload = encode(arguments)
        fingerprint = hashlib.sha256(encode([tool, arguments]).encode()).hexdigest()
        opid = uuid.uuid5(uuid.NAMESPACE_URL, encode([self.session_id, intent, slot])).hex
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO operations "
                       "(operation_id,intent,slot,tool,payload,fingerprint,state) VALUES (?,?,?,?,?,?,?)",
                       (opid, intent, slot, tool, payload, fingerprint, "PREPARED"))
            row = dict(db.execute("SELECT * FROM operations WHERE intent=? AND slot=?", (intent, slot)).fetchone())
            if row["operation_id"] != opid or row["fingerprint"] != fingerprint:
                raise OperationConflict("Approved operation arguments changed; a new call ID is not a new intent")
        return row

    def get(self, operation_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown business operation")
        return dict(row)

    def has_unknown(self, intent):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM operations WHERE intent=? AND state='UNKNOWN' LIMIT 1",
                              (intent,)).fetchone() is not None

    def bind(self, operation_id, call_identity):
        with self.connect() as db:
            key = encode(call_identity)
            db.execute("INSERT OR IGNORE INTO bindings VALUES (?,?)", (key, operation_id))
            existing = db.execute("SELECT operation_id FROM bindings WHERE call_identity=?", (key,)).fetchone()
            if existing[0] != operation_id:
                raise OperationConflict("Call identity already belongs to another operation")

    def for_call(self, call_identity):
        with self.connect() as db:
            row = db.execute("SELECT operation_id FROM bindings WHERE call_identity=?",
                             (encode(call_identity),)).fetchone()
        return self.get(row[0]) if row else None

    def dispatch(self, operation_id):
        with self.connect() as db:
            return db.execute("UPDATE operations SET state='UNKNOWN' "
                              "WHERE operation_id=? AND state='PREPARED'", (operation_id,)).rowcount == 1

    def complete(self, operation_id, result, evidence="confirmed adapter response"):
        value = encode(result)
        with self.connect() as db:
            if db.execute("UPDATE operations SET state='SUCCEEDED',result=?,evidence=? "
                          "WHERE operation_id=? AND state='UNKNOWN'", (value, evidence, operation_id)).rowcount != 1:
                raise OperationConflict("Completion does not match an unresolved dispatched operation")

    def reconcile(self, operation_id, resolution, evidence, *, result=None, seq=0):
        if resolution not in {"succeeded", "not_applied", "failed"} or not evidence.strip():
            raise ValueError("Explicit resolution and evidence are required")
        with self.connect() as db:
            row = dict(db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone())
            if seq and seq <= row["reconciled_seq"]:
                return
            if row["state"] == "SUCCEEDED":
                if resolution != "succeeded":
                    raise OperationConflict("Confirmed completion cannot be reset for replay")
                # Preserve the original response when a native reconciliation is replayed.
                db.execute("UPDATE operations SET reconciled_seq=MAX(reconciled_seq,?) WHERE operation_id=?",
                           (seq, operation_id))
                return
            state = {"succeeded": "SUCCEEDED", "not_applied": "PREPARED", "failed": "FAILED"}[resolution]
            payload = encode(result if result is not None else {"verified": resolution, "evidence": evidence})
            db.execute("UPDATE operations SET state=?,result=?,evidence=?,reconciled_seq=? WHERE operation_id=?",
                       (state, payload, evidence, seq, operation_id))


def validate_native_resolution(store, operation, resolution):
    """Reject contradictory manual evidence before the native gate is cleared."""
    from backend.conversation.snapshot import identity

    if getattr(store, "database", None):
        return
    if not (store.directory / "business-operations.sqlite3").exists():
        return
    row = OperationJournal(store.directory, store.session_id).for_call(identity(operation, "call_id"))
    if row and row["state"] == "SUCCEEDED" and resolution != "succeeded":
        raise OperationConflict("A durable success receipt already exists; cannot mark this operation not applied or failed")
