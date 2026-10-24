"""HTTP adapters: validate inputs and delegate to domain owners."""

from pathlib import Path

from fastapi import HTTPException, Query, Request

from backend.models.reasoning import policy as reasoning_policy
from backend.tools.base import ToolError

from .assets import asset_router
from .diagnostics import diagnostics_router
from .evaluations import evaluation_router
from .events import event_response
from .models import ApprovalInput, DirectoryInput, MessageInput, PromptInput, SkillInput
from .modes import mode_router
from .nonblocking import Router
from .snapshots import snapshot_router
from .storage import storage_router
from .swe_bench import swe_bench_router


def router(service):
    from backend.evaluation.recovery_study import recovery_study_router

    api = Router(prefix="/api")
    api.include_router(recovery_study_router(service))
    api.include_router(swe_bench_router(service))
    api.include_router(storage_router(service))

    def session(sid):
        try:
            return service.get(sid)
        except KeyError:
            raise HTTPException(404, "会话不存在") from None

    def idle(item):
        if item.busy():
            raise HTTPException(409, "执行中无法变更目录、提示词或技能，请先停止任务")

    @api.get("/info")
    async def info():
        return {
            "model": service.config.model,
            "reasoning": reasoning_policy(service.config),
            "shell": service.config.shell,
            "sandbox_available": service.sandbox.available(),
            "sandbox_reason": service.sandbox.reason,
            "storage": {"active_mode": service.config.storage_mode, "connected": bool(service.database)},
        }

    @api.get("/sessions")
    async def sessions():
        archived = service.experiments.session_ids()
        return [{**entry, "recovery_study": service.recovery_study.metadata(entry['session_id'])}
                for entry in service.list_sessions() if entry['session_id'] not in archived]

    @api.post("/sessions", status_code=201)
    async def create():
        return service.create().detail()

    @api.get("/sessions/{sid}")
    async def detail(sid: str):
        return {**session(sid).detail(), "recovery_study": service.recovery_study.metadata(sid)}

    @api.post("/sessions/{sid}/runs", status_code=202)
    async def run(sid: str, body: MessageInput):
        if not body.text.strip():
            raise HTTPException(422, "请输入任务")
        service.recovery_study.ensure_live(sid)
        return {"run_id": session(sid).start(body.text, body.reasoning_effort, body.agent_mode)}

    @api.post("/sessions/{sid}/cancel")
    async def cancel(sid: str):
        item = session(sid)
        await item.cancel()
        return item.detail()

    @api.get("/sessions/{sid}/events")
    async def events(sid: str, request: Request, after: int = Query(default=0, ge=0)):
        return event_response(session(sid), after, request)

    @api.post("/sessions/{sid}/approvals/{ticket}")
    async def approve(sid: str, ticket: str, body: ApprovalInput):
        session(sid).approvals.resolve(ticket, body.run_id, body.approved)
        return {"accepted": True}

    @api.post("/sessions/{sid}/directories")
    async def directories(sid: str, body: DirectoryInput):
        item = session(sid)
        idle(item)
        operation = {
            "add": item.permissions.add,
            "remove": item.permissions.remove,
            "select": item.permissions.select,
        }[body.action]
        operation(body.path)
        item.changed.set()
        return item.permissions.listing()

    @api.get("/memory")
    async def memories():
        return service.long_memory.list()

    @api.delete("/memory/{entry_id}")
    async def delete_memory(entry_id: str):
        if not service.long_memory.delete(entry_id):
            raise HTTPException(404, "记忆不存在")
        return {"deleted": True}

    @api.get("/sessions/{sid}/skills")
    async def skills(sid: str):
        item = session(sid)
        return item.executor.skills.list() + item.executor.skills.list("project")

    @api.post("/sessions/{sid}/skills")
    async def select_skill(sid: str, body: SkillInput):
        item = session(sid)
        idle(item)
        item.selected_skill = item.executor.force_skill(body.name, body.scope, body.project_path)
        return {"selected": body.name}

    @api.get("/sessions/{sid}/prompts")
    async def prompts(sid: str):
        item = session(sid)
        assets = item.executor.assets
        return [
            {
                "scope": "global",
                "project_path": None,
                "path": str(assets.global_path()),
                "content": assets.global_text(),
            }
        ] + [
            {
                "scope": "project",
                "project_path": str(root),
                "path": str(assets.project_path(root)),
                "content": assets.project_text(root),
            }
            for root in item.permissions.roots()
        ]

    @api.put("/sessions/{sid}/prompts")
    async def save_prompt(sid: str, body: PromptInput):
        item = session(sid)
        if any(other.busy() for other in service.sessions.values()):
            raise HTTPException(409, "有任务正在执行，请停止后编辑提示词")
        assets = item.executor.assets
        if body.scope == "project":
            root = Path(body.project_path or "").resolve()
            if root not in item.permissions.roots():
                raise ToolError("项目目录尚未授权", "path")
            path = assets.project_path(root)
        else:
            path = assets.global_path()
        assets.save_prompt(path, body.content)
        return {"saved": True}

    api.include_router(snapshot_router(service, session))
    api.include_router(diagnostics_router(service, session))
    api.include_router(evaluation_router(service, session))
    api.include_router(asset_router(service, session, idle))
    api.include_router(mode_router(service, session))
    return api
