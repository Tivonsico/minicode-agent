"""User-facing folder and skill management adapters."""

import httpx
from fastapi import HTTPException, Query

from backend.permissions.browser import DirectoryBrowser
from backend.skills.library import SkillLibrary
from backend.skills.repository import bundle, discover
from backend.skills.search import search
from backend.tools.base import ToolError

from .models import SkillCreateInput, SkillInstallInput, SkillRepositoryInput, SkillTextInput
from .nonblocking import Router


def asset_router(service, session, idle):
    api = Router(prefix="/sessions/{sid}")

    def editable(item, scope):
        idle(item)
        if scope == "global" and any(other.busy() for other in service.sessions.values()):
            raise HTTPException(409, "有任务正在执行，请停止后修改全局技能")

    @api.get("/directory-browser")
    async def folders(sid: str, path: str | None = Query(default=None, max_length=4096)):
        item = session(sid)
        idle(item)
        return DirectoryBrowser(item.permissions).browse(path)

    @api.post("/skill-library")
    async def create(sid: str, body: SkillCreateInput):
        item = session(sid)
        editable(item, body.scope)
        return SkillLibrary(item.executor.skills).create(
            body.name,
            body.description,
            body.content,
            body.scope,
            body.project_path,
        )

    @api.get("/skill-library/{name}")
    async def read(sid: str, name: str, scope: str = "global", project_path: str | None = None):
        item = session(sid)
        idle(item)
        return {"content": item.executor.skills.load(name, scope, project_path)}

    @api.put("/skill-library/{name}")
    async def update(sid: str, name: str, body: SkillTextInput):
        item = session(sid)
        editable(item, body.scope)
        item.executor.skills.save(name, body.content, body.scope, body.project_path)
        return {"saved": True}

    @api.get("/skill-search")
    async def find(sid: str, q: str = Query(min_length=2, max_length=100)):
        idle(session(sid))
        async with httpx.AsyncClient(follow_redirects=False) as client:
            return await search(client, q)

    @api.post("/skill-repository")
    async def inspect(sid: str, body: SkillRepositoryInput):
        idle(session(sid))
        async with httpx.AsyncClient(follow_redirects=False) as client:
            return await discover(client, body.url, skill_name=body.skill_name)

    @api.post("/skill-download")
    async def download(sid: str, body: SkillInstallInput):
        item = session(sid)
        editable(item, body.scope)
        if body.scope == "project":
            item.executor.skills._directory(body.scope, body.project_path)
        async with httpx.AsyncClient(follow_redirects=False) as client:
            files = await bundle(client, body.repository, body.revision, body.path)
        # Tasks or grants can change while the download is in flight.
        editable(item, body.scope)
        try:
            return SkillLibrary(item.executor.skills).install(
                body.name,
                files,
                body.scope,
                body.project_path,
            )
        except OSError as error:
            raise ToolError("无法保存技能，请检查文件夹权限或同名技能。", "skill_path") from error

    return api
