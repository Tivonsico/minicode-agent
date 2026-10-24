"""User-only file history and reviewed restore endpoints; never model tools."""

import asyncio
import uuid

from fastapi import HTTPException
from pydantic import Field

from backend.conversation.operations import unresolved_operations
from backend.observability.scope import scope
from backend.snapshots.lifecycle import file_io, workspace_history_gate

from .models import Payload
from .nonblocking import Router


class RestoreInput(Payload):
    token: str = Field(pattern="^[a-f0-9]{64}$")
    accept_conflicts: bool = False


class PinInput(Payload):
    pinned: bool


def snapshot_router(service, session):
    api = Router(prefix="/sessions/{sid}/file-snapshots")

    async def resolve(item, project_id, round_id):
        version = item.permissions.version
        repository, record = await file_io(item.file_snapshots.resolve, project_id, round_id)
        check_grant(item, version)
        return repository, record, version

    def check_grant(item, version):
        if item.permissions.version != version:
            raise ValueError("目录授权已变化，请刷新文件历史")

    @api.get("")
    async def listing(sid: str):
        item = session(sid)
        return {"snapshots": await file_io(item.file_snapshots.listing), "keep": 20,
                "coverage": "仅授权目录中的普通文件；遵循 .gitignore，跳过敏感文件、链接和超过 8 MiB 的文件。不会恢复数据库、网络或其他外部操作。"}

    @api.get("/{project_id}/{round_id}")
    async def changes(sid: str, project_id: str, round_id: str):
        item = session(sid)
        repository, record, version = await resolve(item, project_id, round_id)
        changes = await file_io(repository.changes, round_id)
        check_grant(item, version)
        return {"snapshot": record, **changes}

    def ready(item):
        if item.busy() or getattr(item, "_restoring_files", False):
            raise HTTPException(409, "任务或文件恢复正在执行，请等待结束")
        if item.store.failed or unresolved_operations(item.store.view):
            raise HTTPException(409, "有待核实操作或存储状态未确认，请先在检查面板核实")

    @api.post("/{project_id}/{round_id}/preview")
    async def preview(sid: str, project_id: str, round_id: str):
        item = session(sid)
        ready(item)
        repository, _, version = await resolve(item, project_id, round_id)
        async with workspace_history_gate([repository.project]):
            ready(item)
            check_grant(item, version)
            result = await file_io(repository.preview_restore, round_id)
            check_grant(item, version)
            return result

    @api.post("/{project_id}/{round_id}/restore")
    async def restore(sid: str, project_id: str, round_id: str, body: RestoreInput):
        item = session(sid)
        ready(item)
        repository, record, version = await resolve(item, project_id, round_id)
        ready(item)
        item._restoring_files = True
        try:
            async with workspace_history_gate([repository.project]):
                check_grant(item, version)
                async with item.executor.execution_gate() as lease:
                    # Revalidate before recording any operation or modifying files.
                    plan = await file_io(repository.preview_restore, round_id)
                    if body.token != plan["token"]:
                        raise ValueError("预览后项目文件发生变化，请重新预览恢复")
                    if plan["conflicts"] and not body.accept_conflicts:
                        raise ValueError("存在后续或人工修改，必须明确确认覆盖这些文件")
                    cid = uuid.uuid4().hex
                    with scope(item.store, run_id="restore-" + cid, turn_id=record["turn_id"],
                               request_id=None, call_id=cid):
                        item.publish("tool_start", call_id=cid, name="RestoreFiles",
                                     input={"project_id": project_id, "round_id": round_id})
                        if service.database:
                            item.publish("operation_prepared", call_id=cid, name="RestoreFiles")
                        item.publish("tool_running", call_id=cid, name="RestoreFiles")
                        lease.before_effect(item.store, cid)
                        loop = asyncio.get_running_loop()
                        def record_backup(backup):
                            # The worker confirms the durable association before its
                            # first file effect; no DB transaction encloses file I/O.
                            item.store.append("file_snapshot_restore_backup", snapshot=backup)
                            loop.call_soon_threadsafe(item.changed.set)
                        outcome = "unknown"
                        try:
                            backup = await file_io(repository.restore, round_id, body.token,
                                                   accept_conflicts=body.accept_conflicts,
                                                   before_effect=record_backup)
                        except BaseException:
                            # A cancellation can arrive after disk writes completed. Do
                            # not guess; retain the execution lease for reconciliation.
                            item.publish("tool_result", call_id=cid, name="RestoreFiles",
                                         status="unknown", content="恢复中断或失败；恢复前副本已保留，请核查文件与恢复记录。")
                            raise
                        else:
                            item.publish("tool_result", call_id=cid, name="RestoreFiles",
                                         status="succeeded", content="已恢复到该轮开始前的文件状态")
                            outcome = "succeeded"
                        finally:
                            item.publish("tool_end", call_id=cid, name="RestoreFiles",
                                         status=outcome)
                        lease.completed()
                        item.publish("file_snapshot_restored", snapshot=backup, restored=round_id)
                        return {"backup": backup}
        finally:
            item._restoring_files = False
            item.changed.set()

    @api.post("/{project_id}/{round_id}/pin")
    async def pin(sid: str, project_id: str, round_id: str, body: PinInput):
        item = session(sid)
        ready(item)
        repository, _, version = await resolve(item, project_id, round_id)
        async with workspace_history_gate([repository.project]):
            ready(item)
            check_grant(item, version)
            await file_io(repository.pin, round_id, body.pinned)
            item.publish("file_snapshot_pin", project_id=project_id, snapshot_id=round_id,
                         pinned=body.pinned)
        return {"pinned": body.pinned}

    return api
