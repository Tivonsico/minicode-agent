"""Create a portal at HTTP command entry, before synchronous domain calls."""
import asyncio

import greenback
from fastapi import APIRouter
from fastapi.routing import APIRoute


class StorageRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def run(request):
            service = request.app.state.service
            sid = request.path_params.get("sid")
            if sid and service.database:
                lock = service.command_locks.setdefault(sid, asyncio.Lock())
                async with lock:
                    return await greenback.with_portal_run(handler, request)
            return await greenback.with_portal_run(handler, request)
        return run


class Router(APIRouter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, route_class=StorageRoute, **kwargs)
