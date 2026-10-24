"""Opt-in durable tools use the native execution gate and UNKNOWN protocol.

Adapters declare stable-key retry or authoritative probing explicitly. A negative
eventually-consistent search must return UNKNOWN, never ABSENT. Arbitrary Shell,
file tools and Gmail are not silently assigned these capabilities.
"""
import asyncio
import json
from dataclasses import dataclass

from backend.conversation.operations import unresolved_operations
from backend.conversation.snapshot import identity
from backend.observability.scope import current
from backend.tools.base import Tool, ToolError, ToolResult

from .operations import OperationConflict, OperationJournal


@dataclass(frozen=True)
class ProbeResult:
    status: str  # PRESENT / ABSENT / UNKNOWN
    evidence: str
    result: object = None


@dataclass(frozen=True)
class EffectAdapter:
    send: object
    probe: object = None
    stable_key_retry: bool = False
    barrier: object = None


class GuardedTool:
    def __init__(self, executor, name, intent_id, slot, adapter):
        self.executor, self.name = executor, name
        self.intent_id, self.slot, self.adapter = intent_id, slot, adapter
        self._last_reconciliation_seq = 0
        store = executor.output_store.store
        if getattr(store, "database", None):
            raise ValueError("Durable adapter journal currently supports local sessions only")
        self.journal = OperationJournal(store.directory, store.session_id)

    async def barrier(self, stage, operation):
        if self.adapter.barrier:
            await self.adapter.barrier(stage, operation)

    def sync_reconciliations(self):
        # Native reconciliation is the audit authority. Replaying it repairs a
        # crash between the native event commit and this derived journal update.
        for event in self.executor.output_store.store.view:
            if event["type"] != "operation_reconciled" or event["seq"] <= self._last_reconciliation_seq:
                continue
            row = self.journal.for_call(identity(event, "call_id"))
            if row:
                self.journal.reconcile(row["operation_id"], event["resolution"], event["evidence"], seq=event["seq"])
            self._last_reconciliation_seq = event["seq"]

    async def execute(self, cid, arguments):
        executor = self.executor
        decision = executor.permissions.decide(self.name, mode=executor.agent_mode)
        from backend.permissions.policy import Action
        if decision.action == Action.DENY:
            raise ToolError("Durable tool denied by execution mode", "permission")
        try:
            self.sync_reconciliations()
            row = self.journal.prepare(self.intent_id, self.slot(arguments), self.name, arguments)
        except (OperationConflict, ValueError) as exc:
            return ToolResult(cid, self.name, "failed", str(exc), "operation_conflict")
        opid = row["operation_id"]
        self.journal.bind(opid, identity({**current(executor.output_store.store), "call_id": cid}, "call_id"))
        executor.emit("business_operation_bound", call_id=cid, name=self.name, operation_id=opid,
                      intent_id=self.intent_id, slot=row["slot"], fingerprint=row["fingerprint"])
        if row["state"] == "SUCCEEDED":
            return ToolResult(cid, self.name, "succeeded", row["result"])
        if row["state"] == "FAILED":
            return ToolResult(cid, self.name, "failed", row["result"] or "Operation rejected", "operation_failed")
        if row["state"] == "UNKNOWN":
            return ToolResult(cid, self.name, "unknown", f"Operation {opid} outcome UNKNOWN; reconcile before continuing",
                              "termination_unknown")
        if not await executor._approval(cid, self.name, arguments, "Business operation: " + opid):
            return ToolResult(cid, self.name, "rejected", "User rejected business operation")
        executor.mark_running(cid, self.name)
        await self.barrier("before_dispatch", row)
        if not self.journal.dispatch(opid):
            return ToolResult(cid, self.name, "unknown", "Operation already dispatched", "termination_unknown")
        try:
            await self.barrier("after_dispatch", row)
            result = await self.adapter.send(opid, arguments)
            await self.barrier("before_result_record", row)
            self.journal.complete(opid, result)
            await self.barrier("after_result_record", row)
        except asyncio.CancelledError as exc:
            exc.tool_status = "unknown"
            exc.tool_content = f"Operation {opid} may have completed; do not replay"
            raise
        except Exception as exc:
            raise ToolError(f"Operation {opid} outcome UNKNOWN ({type(exc).__name__}); do not replay",
                            "termination_unknown") from exc
        return ToolResult(cid, self.name, "succeeded", json.dumps(result, ensure_ascii=False))


def register_guarded_tool(executor, *, name, description, schema, intent_id, slot, adapter):
    guarded = GuardedTool(executor, name, intent_id, slot, adapter)
    executor.registry.register(Tool(name, description, schema, guarded.execute, False, True, "durable_effect"))
    return guarded


async def recover_operations(session, tools, *, processes_stopped):
    """Reconcile only configured adapters after explicit old-executor termination.

    This entry is for the runtime/controller, never exposed as a model tool.
    The native journal remains the reconciliation authority and is committed
    before clearing its execution marker. Interrupted recovery stays UNKNOWN.
    """
    if session.busy() or not processes_stopped:
        raise ValueError("Stop and confirm the old executor before recovery")
    session._recovering_operations = True
    decisions = []
    try:
        async with session.executor.execution_gate(reconciliation=True) as lease:
            for unknown in unresolved_operations(session.store.view):
                guarded = tools.get(unknown.get("name"))
                row = guarded.journal.for_call(identity(unknown, "call_id")) if guarded else None
                if row is None:
                    decisions.append({"result_seq": unknown["seq"], "decision": "needs_user"})
                    continue
                opid = row["operation_id"]
                resolution = "succeeded"
                evidence = "Durable confirmed response: " + opid
                if row["state"] == "PREPARED":
                    resolution = "not_applied"
                    evidence = "Dispatch claim was never committed: " + opid
                elif row["state"] == "UNKNOWN":
                    adapter = guarded.adapter
                    try:
                        if adapter.stable_key_retry:
                            result = await adapter.send(opid, json.loads(row["payload"]))
                            evidence = "Receiver honors original stable key: " + opid
                        elif adapter.probe:
                            probe = await adapter.probe(opid, json.loads(row["payload"]))
                            if not isinstance(probe, ProbeResult) or not probe.evidence.strip():
                                raise ValueError("Authoritative probe evidence is required")
                            if probe.status == "PRESENT":
                                result, evidence = probe.result, probe.evidence
                            elif probe.status == "ABSENT":
                                resolution, evidence = "not_applied", probe.evidence
                            else:
                                decisions.append({"operation_id": opid, "decision": "needs_user"})
                                continue
                        else:
                            decisions.append({"operation_id": opid, "decision": "needs_user"})
                            continue
                        if resolution == "succeeded":
                            guarded.journal.complete(opid, result, evidence)
                        else:
                            guarded.journal.reconcile(opid, resolution, evidence)
                    except Exception as exc:
                        decisions.append({"operation_id": opid, "decision": "unknown",
                                          "error_type": type(exc).__name__})
                        continue
                elif row["state"] != "SUCCEEDED":
                    decisions.append({"operation_id": opid, "decision": "needs_user"})
                    continue
                session.store.append(
                    "operation_reconciled", result_seq=unknown["seq"], resolution=resolution,
                    evidence=evidence, actor="harness_recovery", processes_stopped=True,
                    operation_id=opid,
                    **{k: unknown.get(k) for k in ("turn_id", "run_id", "request_id", "call_id", "name")},
                )
                lease.clear_for(session.store, unknown)
                decisions.append({"operation_id": opid, "decision": resolution})
            remaining = unresolved_operations(session.store.view)
            if not remaining and session.executor.pause_reason == "termination_unknown":
                session.executor.terminal_failed, session.executor.pause_reason = False, None
            session.changed.set()
            return {"unresolved_operations": remaining, "decisions": decisions}
    finally:
        session._recovering_operations = False
