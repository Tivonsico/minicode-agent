"""Validate storage configuration and recover abandoned session ownership explicitly."""

import asyncio

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.storage.mysql import MySQLDatabase
from backend.storage.settings import candidate, ensure_mode_switch_safe, save_settings, status

from .nonblocking import Router


class StorageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage_mode: str
    mysql_host: str = ""
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_database: str = ""
    mysql_user: str = ""
    mysql_password: str = ""
    mysql_ssl_ca: str = ""
    mysql_pool_size: int = Field(default=8, ge=1, le=64)


class OwnerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_owner: str
    evidence: str = Field(min_length=1, max_length=10000)
    processes_stopped: bool


def storage_router(service):
    api = Router()

    def check(body):
        config = candidate(service.config, body.model_dump())
        if config.storage_mode == "mysql":
            database = MySQLDatabase(config)
            database.close()
        return config

    @api.get("/storage")
    async def storage_status():
        return await asyncio.to_thread(status, service)

    @api.post("/storage/check")
    async def storage_check(body: StorageInput):
        await asyncio.to_thread(check, body)
        return {"checked": True, "mode": body.storage_mode, "active_mode": service.config.storage_mode}

    @api.put("/storage")
    async def configure_storage(body: StorageInput):
        if any(item.busy() for item in service.sessions.values()):
            raise HTTPException(409, "请先停止所有任务，再保存存储配置")
        config = await asyncio.to_thread(check, body)
        owners = (tuple(item.store.owner for item in service.sessions.values())
                  if service.database else ())
        await asyncio.to_thread(ensure_mode_switch_safe, config, service.database,
                                owners)
        save_settings(config)
        return await asyncio.to_thread(status, service)

    @api.post("/sessions/{sid}/owner/reconcile")
    async def reconcile_owner(sid: str, body: OwnerInput):
        await asyncio.to_thread(service.recover_owner, sid, body.expected_owner,
                                body.evidence, body.processes_stopped)
        return {"released": True}

    return api
