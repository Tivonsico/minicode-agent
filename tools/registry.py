from .base import Tool, ToolError


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def register(self, tool):
        if tool.name() in self.tools:
            raise ValueError("Duplicate tool")
        self.tools[tool.name()] = tool

    def get(self, name):
        if name not in self.tools:
            raise ToolError("Unknown tool: " + str(name), "unknown_tool")
        return self.tools[name]

    def schemas(self):
        return [tool.api_schema() for tool in self.tools.values()]


def object_schema(properties, required):
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def build_registry(handler):
    registry = ToolRegistry()
    string = {"type": "string"}
    path = {"type": "string", "minLength": 1}
    definitions = [
        (
            "ReadFile",
            "Read a UTF-8 file with line numbers. Read before modifying existing files.",
            {
                "path": path,
                "start_line": {"type": "integer", "minimum": 1},
                "line_count": {"type": "integer", "minimum": 1, "maximum": 2000},
            },
            ["path"],
            True,
            False,
            "file",
        ),
        (
            "Glob",
            "List matching files and directories. * stays within one directory; ** searches recursively. Sensitive entries are omitted.",
            {"pattern": string},
            ["pattern"],
            True,
            False,
            "search",
        ),
        (
            "Grep",
            "Search file contents for a literal string and return paths and line numbers.",
            {"pattern": {"type": "string", "minLength": 1}, "glob": string},
            ["pattern"],
            True,
            False,
            "search",
        ),
        (
            "WriteFile",
            "Create or overwrite a UTF-8 file after approval; existing files require a current read.",
            {"path": path, "content": string},
            ["path", "content"],
            False,
            False,
            "file",
        ),
        (
            "EditFile",
            "Replace exactly one occurrence of old_text after approval; first read the file.",
            {"path": path, "old_text": {"type": "string", "minLength": 1}, "new_text": string},
            ["path", "old_text", "new_text"],
            False,
            False,
            "file",
        ),
        (
            "DeleteFile",
            "Delete one regular file after user confirmation; directories are forbidden.",
            {"path": path},
            ["path"],
            False,
            True,
            "file",
        ),
        (
            "Shell",
            "Run PowerShell after approval only when verified OS isolation is available; otherwise denied.",
            {
                "command": {"type": "string", "minLength": 1},
                "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 86400},
            },
            ["command"],
            False,
            True,
            "shell",
        ),
        (
            "UseSkill",
            "Load installed skills based on descriptions. scope is global by default; for project skills set scope=project and project_path to an authorized directory. Multiple names may be used together.",
            {"names": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 8, "uniqueItems": True},
             "scope": {"type": "string", "enum": ["global", "project"]},
             "project_path": {"type": "string"}},
            ["names"],
            False,
            False,
            "skill",
        ),
        (
            "ReadSkillResource",
            "Read text inside a skill already used this turn. Match its scope and project_path. Scripts are never executed by this tool.",
            {"name": {"type": "string", "minLength": 1}, "path": {"type": "string", "minLength": 1},
             "scope": {"type": "string", "enum": ["global", "project"]},
             "project_path": {"type": "string"}},
            ["name", "path"],
            True,
            False,
            "skill",
        ),
    ]
    for name, description, props, required, readonly, destructive, category in definitions:

        async def invoke(context, input, name=name):
            return await handler(name, context, input)

        registry.register(
            Tool(
                name,
                description,
                object_schema(props, required),
                invoke,
                readonly,
                destructive,
                category,
            )
        )
    return registry
