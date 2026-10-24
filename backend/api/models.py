from pydantic import BaseModel, ConfigDict, Field

from backend.modes.catalog import AgentMode


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageInput(Payload):
    text: str = Field(min_length=1, max_length=100_000)
    reasoning_effort: str | None = Field(default=None, max_length=32)
    agent_mode: AgentMode | None = None


class DirectoryInput(Payload):
    path: str = Field(min_length=1, max_length=4096)
    action: str = Field(pattern="^(add|remove|select)$")


class ApprovalInput(Payload):
    run_id: str
    approved: bool


class PromptInput(Payload):
    scope: str = Field(pattern="^(global|project)$")
    project_path: str | None = None
    content: str = Field(max_length=65536)


class SkillInput(Payload):
    name: str
    scope: str = Field(default="global", pattern="^(global|project)$")
    project_path: str | None = None


class SkillTextInput(Payload):
    scope: str = Field(default="global", pattern="^(global|project)$")
    project_path: str | None = None
    content: str = Field(min_length=1, max_length=65536)


class SkillCreateInput(SkillTextInput):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)


class SkillRepositoryInput(Payload):
    url: str = Field(min_length=1, max_length=2048)
    skill_name: str | None = Field(default=None, max_length=100)


class SkillInstallInput(Payload):
    repository: str = Field(min_length=3, max_length=200)
    revision: str = Field(pattern="^[a-f0-9]{40}$")
    path: str = Field(max_length=2048)
    name: str = Field(min_length=1, max_length=64)
    scope: str = Field(default="global", pattern="^(global|project)$")
    project_path: str | None = None


class ReconcileInput(Payload):
    processes_stopped: bool
    result_seq: int = Field(ge=1)
    resolution: str = Field(pattern="^(succeeded|failed|not_applied)$")
    evidence: str = Field(min_length=1, max_length=10000)


class MemoryEditInput(Payload):
    content: str = Field(min_length=1, max_length=10000)
    version: int = Field(ge=1)


class MemoryCreateInput(Payload):
    session_id: str
    content: str = Field(min_length=1, max_length=10000)
