"""Same-origin browser application; no provider credential is sent to the UI."""

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.runtime.service import RuntimeService
from backend.tools.base import ToolError

from .routes import router
from .security import LocalSecurity


def create_app(config, provider=None, *, initialize_sandbox=True, database=None):
    service = RuntimeService(config, provider, database=database)
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        if initialize_sandbox:
            await service.initialize()
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(
        title="minicode local runtime",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.service, app.state.token = service, token
    app.add_middleware(LocalSecurity, token=token)

    @app.exception_handler(ToolError)
    async def tool_error(request: Request, exc: ToolError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(OSError)
    async def storage_error(request: Request, exc: OSError):
        return JSONResponse({"detail": "存储当前不可用，请重新打开会话核查；没有切换到其他存储。"}, status_code=503)

    @app.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    app.include_router(router(service))
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    if not frontend.is_dir():
        import sys

        frontend = Path(sys.prefix) / "share" / "minicode" / "frontend"
    app.mount("/assets", StaticFiles(directory=frontend), name="assets")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (
            (frontend / "index.html")
            .read_text(encoding="utf-8")
            .replace("__MINICODE_TOKEN__", token)
        )

    return app
