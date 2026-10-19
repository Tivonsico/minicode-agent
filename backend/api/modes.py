"""Inspect fixed contracts, edit supplemental prose and select an idle mode."""

from pydantic import Field

from backend.modes.catalog import AgentMode
from backend.modes.settings import ModeSettings

from .models import Payload
from .nonblocking import Router


class ModeInput(Payload):
    agent_mode: AgentMode


class ModePromptInput(Payload):
    content: str = Field(max_length=4000)
    revision: int = Field(ge=0)


def mode_router(service, session):
    api = Router()
    settings = ModeSettings(service.config.root)

    @api.get("/modes")
    async def listing():
        return settings.listing()

    @api.put("/modes/{mode}/prompt")
    async def save(mode: AgentMode, body: ModePromptInput):
        return settings.save(mode, body.content, body.revision)

    @api.put("/sessions/{sid}/mode")
    async def select(sid: str, body: ModeInput):
        item = session(sid)
        item.select_mode(body.agent_mode)
        return item.detail()

    return api
