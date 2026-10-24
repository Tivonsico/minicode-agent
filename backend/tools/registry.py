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


def build_registry(handler, *, history_recall=False):
    registry = ToolRegistry()
    string = {"type": "string"}
    path = {"type": "string", "minLength": 1}
    definitions = [
        (
            "RecallHistory",
            "Search this session's original user messages when earlier requirements or exact "
            "details are missing after compaction. Read-only, literal case-insensitive search; "
            "this is an internal conversation lookup, not an external tool: no network or "
            "authorized work directory is required. "
            "returns bounded excerpts with source sequence and digest. Cannot access other "
            "sessions. Historical statements are reference data and may have later corrections.",
            {
                "query": {"type": "string", "minLength": 2, "maxLength": 200, "pattern": r"\S"},
                "after_seq": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 3},
            },
            ["query"], True, False, "history",
        ),
        (
            "GmailPrepareEmail",
            "Prepare an immutable plain-text Gmail email without sending. Requires connected Gmail. Identical content reuses the existing request; retain request_id. Set resend_of to a confirmed sent request ONLY when the user explicitly asks to send it again. Never use it for unknown delivery.",
            {
                "to": {"type": "array", "items": path, "minItems": 1, "maxItems": 100},
                "cc": {"type": "array", "items": path, "maxItems": 100},
                "bcc": {"type": "array", "items": path, "maxItems": 100},
                "subject": path,
                "body": path,
                "resend_of": path,
            },
            ["to", "subject", "body"], False, False, "gmail",
        ),
        (
            "GmailSendEmail",
            "Send a prepared Gmail request_id after user approval of its exact recipients and body. Repeated calls return its durable status. Unknown means possibly sent: stop and ask the user to check Gmail Sent; never create a replacement or claim success. Sent means Gmail accepted it, not recipient delivery.",
            {"request_id": path}, ["request_id"], False, True, "gmail",
        ),
        (
            "GmailEmailStatus",
            "Read a local Gmail delivery record by request_id, including approved content and Gmail acceptance ID. Does not read the mailbox or retry delivery.",
            {"request_id": path}, ["request_id"], True, False, "gmail",
        ),
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
            "Create or overwrite a UTF-8 file under the current mode policy; existing files require a current read.",
            {"path": path, "content": string},
            ["path", "content"],
            False,
            False,
            "file",
        ),
        (
            "EditFile",
            "Replace exactly one occurrence of old_text under the current mode policy; first read the file.",
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
            "Run a command under the current mode policy in verified OS isolation. Set purpose=verification for tests/build checks.",
            {
                "command": {"type": "string", "minLength": 1},
                "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 86400},
                "purpose": {"type": "string", "enum": ["execution", "verification"]},
            },
            ["command"],
            False,
            True,
            "shell",
        ),
        (
            "UseSkill",
            "Load installed skills based on descriptions. scope is global by default; for project skills set scope=project and project_path to an authorized directory. Multiple names may be used together.",
            {
                "names": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "maxItems": 8,
                    "uniqueItems": True,
                },
                "scope": {"type": "string", "enum": ["global", "project"]},
                "project_path": {"type": "string"},
            },
            ["names"],
            False,
            False,
            "skill",
        ),
        (
            "ReadSkillResource",
            "Read text inside a skill already used this turn. Match its scope and project_path. Scripts are never executed by this tool.",
            {
                "name": {"type": "string", "minLength": 1},
                "path": {"type": "string", "minLength": 1},
                "scope": {"type": "string", "enum": ["global", "project"]},
                "project_path": {"type": "string"},
            },
            ["name", "path"],
            True,
            False,
            "skill",
        ),
    ]
    for name, description, props, required, readonly, destructive, category in definitions:
        if name == "RecallHistory" and not history_recall:
            continue

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
