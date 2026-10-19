"""Tool contracts, independent of provider and terminal implementations."""

from collections.abc import Callable
from dataclasses import asdict, dataclass

from jsonschema import Draft202012Validator


class ToolError(Exception):
    def __init__(self, message: str, error_type: str = "validation"):
        super().__init__(message)
        self.error_type = error_type


@dataclass
class ToolResult:
    call_id: str
    name: str
    status: str
    content: str
    error_type: str | None = None

    def to_dict(self):
        return asdict(self)

    def as_message(self):
        return {"role": "tool", "tool_call_id": self.call_id, "content": self.content}


@dataclass
class Tool:
    tool_name: str
    help: str
    schema: dict
    handler: Callable
    read_only: bool = False
    destructive: bool = False
    tool_category: str = "file"

    def name(self):
        return self.tool_name

    def description(self):
        return self.help

    def inputSchema(self):
        return self.schema

    def isReadOnly(self):
        return self.read_only

    def isDestructive(self):
        return self.destructive

    def isConcurrencySafe(self, input):
        return self.read_only

    def category(self):
        return self.tool_category

    def validateInput(self, input):
        errors = list(Draft202012Validator(self.schema).iter_errors(input))
        return "; ".join(error.message for error in errors) if errors else None

    async def execute(self, context, input):
        return await self.handler(context, input)

    def api_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.tool_name,
                "description": self.help,
                "parameters": self.schema,
            },
        }
