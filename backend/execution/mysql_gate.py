"""Persistent workspace claims. No expiry and no transaction across tool execution."""
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from backend.storage.mysql import encoded
from backend.storage.nonblocking import io_method
from backend.tools.base import ToolError


def roots_overlap(first, second):
    return any(a == b or a.startswith(b.rstrip("/") + "/")
               or b.startswith(a.rstrip("/") + "/") for a in first for b in second)


class MySQLExecutionLease:
    def __init__(self, store, roots, reconciliation):
        self.store = store
        self.database = store.database
        self.roots = sorted({os.path.normcase(str(Path(p).resolve())).replace("\\", "/") for p in roots})
        self.reconciliation = reconciliation
        self.token = None

    @io_method
    def acquire(self):
        import json

        store = self.store
        token = uuid.uuid4().hex
        def claim(cursor):
            cursor.execute("SELECT id FROM mc_mutex WHERE id=1 FOR UPDATE")
            store._row(cursor)
            if self.reconciliation:
                return None
            cursor.execute("SELECT * FROM mc_claims")
            for row in cursor.fetchall():
                if roots_overlap(self.roots, json.loads(row["roots"])):
                    raise ToolError("工作区被其他批次占用或有待核实操作，请等待或在原会话核实。",
                                    "termination_unknown")
            cursor.execute("INSERT INTO mc_claims VALUES (%s,%s,%s,%s)",
                           (token, store.session_id, store.owner, encoded(self.roots)))
            return token
        self.token = store.database.transaction(claim)
        if self.token:
            store.claim = self.token

    def before_effect(self, store, cid):
        # STARTED and claim ownership are atomically checked by append(tool_running).
        if store is not self.store or not self.token:
            raise OSError("Missing workspace claim")

    def completed(self):
        # A batch can contain several effects. Release only at the batch boundary.
        pass

    def clear_for(self, store, operation):
        if store is not self.store or not self.reconciliation:
            raise ValueError("Invalid reconciliation owner")
        self.release(all_session=True)

    @io_method
    def release(self, *, all_session=False):
        store = self.store
        if store.failed or (not self.token and not all_session):
            return
        def release(cursor):
            cursor.execute("SELECT id FROM mc_mutex WHERE id=1 FOR UPDATE")
            store._row(cursor)
            cursor.execute("SELECT id FROM mc_operations WHERE session_id=%s AND status IN ('STARTED','UNKNOWN')",
                           (store.session_id,))
            if cursor.fetchone():
                return False
            if all_session:
                cursor.execute("DELETE FROM mc_claims WHERE session_id=%s", (store.session_id,))
            else:
                cursor.execute("DELETE FROM mc_claims WHERE id=%s AND owner=%s", (self.token, store.owner))
            return True
        store.database.transaction(release)
        store.claim = None


@asynccontextmanager
async def mysql_execution_gate(store, roots, *, reconciliation=False):
    from .gate import execution_gate
    from .mysql_domain import register_domain

    # Shared across MySQL batches, exclusive against the existing local gate.
    async with execution_gate(reconciliation=reconciliation, shared=True):
        async with execution_gate(reconciliation=True, lock_name="domain.lock"):
            register_domain(store.database)
        lease = MySQLExecutionLease(store, roots, reconciliation)
        try:
            lease.acquire()
            yield lease
        finally:
            if not reconciliation:
                lease.release()
