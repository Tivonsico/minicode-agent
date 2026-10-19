"""Bounded coding tasks, acceptance conditions and retrospective rules."""

from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.modes.catalog import AgentMode


def relative_path(value):
    path = PurePosixPath(value)
    if (
        not path.parts
        or value != str(path)
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or ":" in value
    ):
        raise ValueError("任务文件必须使用工作区内的相对路径")
    if any(
        part in {".git", ".minicode", ".env", "project_skills", "project_system_prompt.md"}
        for part in path.parts
    ):
        raise ValueError("任务文件不能覆盖受保护资源")
    return value


class Assertion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    kind: Literal["call", "file_contains", "file_equals", "file_absent"] = "call"
    path: str = "solution.py"
    symbol: str = Field(default="", max_length=100, pattern=r"^[a-zA-Z0-9_]*$")
    args: list[Any] = Field(default_factory=list, max_length=20)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    expected: Any = None
    raises: str | None = Field(default=None, pattern=r"^[A-Za-z]+Error$")
    phase: Literal["target", "regression", "constraint"] = "target"

    @model_validator(mode="after")
    def valid(self):
        relative_path(self.path)
        if self.kind == "call" and not self.symbol:
            raise ValueError("函数验收必须填写函数名")
        if (
            self.kind.startswith("file_")
            and self.kind != "file_absent"
            and not isinstance(self.expected, str)
        ):
            raise ValueError("文件验收的预期值必须是文本")
        return self


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=120)
    input_contains: list[str] = Field(default_factory=list, max_length=20)
    output_contains: list[str] = Field(default_factory=list, max_length=20)
    required_tools: list[str] = Field(default_factory=list, max_length=20)
    forbidden_tools: list[str] = Field(default_factory=list, max_length=20)
    expected_status: str | None = Field(
        default=None, pattern="^(completed|failed|paused|interrupted)$"
    )
    require_verification: bool = False
    agent_mode: AgentMode | None = None
    goal: Literal["implementation", "planning"] = "implementation"
    mode: Literal["history", "task"] = "history"
    category: Literal["history", "repair", "feature", "multiturn", "security"] = "history"
    prompts: list[str] = Field(default_factory=list, max_length=10)
    files: dict[str, str] = Field(default_factory=dict)
    assertions: list[Assertion] = Field(default_factory=list, max_length=100)
    protected_files: list[str] = Field(default_factory=list, max_length=20)
    source: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def meaningful(self):
        for values in (
            self.input_contains,
            self.output_contains,
            self.required_tools,
            self.forbidden_tools,
        ):
            if any(not value.strip() or len(value) > 2000 for value in values):
                raise ValueError("规则文本不能为空或超过 2000 字符")
        if self.mode == "task":
            if not self.prompts or not self.assertions:
                raise ValueError("可运行任务必须有用户需求和独立验收条件")
            if self.goal == "planning" and not self.output_contains:
                raise ValueError("规划评测必须填写计划回复的验收关键词；只读检查不代表计划质量")
            if self.goal == "planning" and any(a.kind == "call" for a in self.assertions):
                raise ValueError("规划评测使用文件约束验收，不能按代码函数实现判分")
            if any(not p.strip() or len(p) > 10000 for p in self.prompts):
                raise ValueError("任务需求不能为空或超过10000字符")
            if len(self.files) > 30 or sum(len(v) for v in self.files.values()) > 100000:
                raise ValueError("初始文件最多30个、合计100000字符")
            for path in self.files:
                relative_path(path)
            for path in self.protected_files:
                relative_path(path)
                if path not in self.files:
                    raise ValueError("受保护文件必须存在于初始文件中")
            return self
        if not (
            self.output_contains
            or self.required_tools
            or self.forbidden_tools
            or self.expected_status
            or self.require_verification
        ):
            raise ValueError("至少填写一个判分条件")
        return self


class DatasetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    revision: int = Field(ge=0)
    builtin_version: int = Field(default=1, ge=0)
    cases: list[Case] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("评测项 ID 不能重复")
        return self


class EvaluationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_ids: list[str] = Field(min_length=1, max_length=50)
    case_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)


class BenchmarkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_ids: list[str] = Field(min_length=1, max_length=100)
    repeats: int = Field(default=1, ge=1, le=5)
    timeout_seconds: int = Field(default=180, ge=15, le=900)
    reasoning_effort: str | None = None
    agent_modes: list[AgentMode] = Field(
        default_factory=lambda: ["auto"], min_length=1, max_length=4
    )
    approval_behavior: Literal["approve", "reject"] = "approve"

    @model_validator(mode="after")
    def unique_modes(self):
        if len(set(self.agent_modes)) != len(self.agent_modes):
            raise ValueError("评测模式不能重复")
        return self


class ImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    revision: int = Field(ge=1)
