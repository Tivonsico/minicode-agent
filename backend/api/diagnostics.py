"""Read execution evidence and explicitly manage state snapshots."""

from fastapi import HTTPException, Query
from fastapi.responses import JSONResponse

from backend.conversation.operations import unresolved_operations
from backend.modes.catalog import tags
from backend.observability.inputs import read_input
from backend.observability.trajectory import export, project

from .models import MemoryCreateInput, MemoryEditInput, ReconcileInput
from .nonblocking import Router


def diagnostics_router(service, session):
    api = Router()

    @api.get("/sessions/{sid}/diagnostics")
    async def diagnostics(sid: str):
        item = session(sid)
        return {
            **project(item.store.events),
            "recovery": item.store.recovery_info,
            "unresolved_operations": unresolved_operations(item.store.view),
            "running": item.busy(),
            "summaries": [e for e in item.store.events if e["type"] == "context_compressed"],
            "milestones": [
                e
                for e in item.store.events
                if e["type"]
                in {
                    "step_start",
                    "step_end",
                    "tool_end",
                    "approval_closed",
                    "verification_result",
                    "completion_check",
                    "request_recovery",
                    "context_compressed",
                    "run_end",
                }
            ],
            "verifications": [e for e in item.store.events if e["type"] == "verification_result"],
        }

    @api.get("/sessions/{sid}/logs")
    async def logs(
        sid: str,
        after: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
        kind: str | None = None,
    ):
        events = session(sid).store.events
        selected = [e for e in events if e["seq"] > after and (kind is None or e["type"] == kind)]
        page = selected[:limit]
        return {
            "events": page,
            "next": page[-1]["seq"] if page else after,
            "has_more": len(selected) > limit,
        }

    @api.get("/sessions/{sid}/inputs")
    async def inputs(sid: str, ref: str):
        try:
            return read_input(session(sid).store, ref)
        except FileNotFoundError:
            raise HTTPException(404, "请求输入不存在") from None

    @api.get("/sessions/{sid}/trajectory")
    async def trajectory(sid: str):
        return JSONResponse(
            export(session(sid).store),
            headers={"Content-Disposition": f'attachment; filename="trajectory-{sid}.json"'},
        )

    @api.post("/sessions/{sid}/operations/reconcile")
    async def reconcile(sid: str, body: ReconcileInput):
        item = session(sid)
        if item.busy() or not body.processes_stopped:
            raise HTTPException(409, "请先停止执行并确认旧进程已终止，再核实结果")
        unknown = next(
            (e for e in unresolved_operations(item.store.view) if e["seq"] == body.result_seq), None
        )
        if unknown is None:
            raise HTTPException(409, "操作状态已经变化，请刷新后再核实")
        if not body.evidence.strip():
            raise HTTPException(422, "必须填写实际核实证据")
        async with item.executor.execution_gate(reconciliation=True) as lease:
            from backend.recovery.operations import validate_native_resolution

            validate_native_resolution(item.store, unknown, body.resolution)
            item.store.append(
                "operation_reconciled",
                result_seq=body.result_seq,
                resolution=body.resolution,
                evidence=body.evidence.strip(),
                actor="local_user",
                processes_stopped=True,
                **{
                    key: unknown.get(key)
                    for key in ("turn_id", "run_id", "request_id", "call_id", "name")
                },
            )
            remaining = unresolved_operations(item.store.view)
            if not remaining and item.executor.pause_reason == "termination_unknown":
                item.executor.terminal_failed, item.executor.pause_reason = False, None
            lease.clear_for(item.store, unknown)
        item.changed.set()
        return {"unresolved_operations": remaining}

    @api.post("/memory", status_code=201)
    async def add_memory(body: MemoryCreateInput):
        item = session(body.session_id)
        if not body.content.strip():
            raise HTTPException(422, "记忆内容不能为空")
        return service.long_memory.add(
            body.content.strip(),
            item.memory.scope(),
            body.session_id,
            identities=tags(item.mode_settings.profile(item.agent_mode)),
        )

    @api.put("/memory/{entry_id}")
    async def edit_memory(entry_id: str, body: MemoryEditInput):
        if not body.content.strip():
            raise HTTPException(422, "记忆内容不能为空")
        try:
            return service.long_memory.edit(entry_id, body.content.strip(), body.version)
        except KeyError:
            raise HTTPException(404, "记忆不存在") from None

    return api
