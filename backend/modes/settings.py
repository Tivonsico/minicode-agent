"""Only supplemental prose is editable; each run freezes its own profile."""

import json

from backend.context.system_prompt import SYSTEM_PROMPT
from backend.conversation.store import atomic_json, internal_directory
from backend.observability.inputs import digest

from .catalog import HARD_BOUNDARIES, MODES, POLICY_VERSION, READ_TOOLS, validate


class ModeSettings:
    def __init__(self, root):
        self.path = internal_directory(root, "modes") / "settings.json"

    def read(self):
        if self.path.is_symlink():
            raise ValueError("模式设置不能使用链接文件")
        return (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {"revision": 0, "notes": {}}
        )

    def profile(self, mode):
        validate(mode)
        data = self.read()
        note = data["notes"].get(mode, "")
        fixed = MODES[mode]
        return {
            "agent_mode": mode,
            "label": fixed["label"],
            "actions": {**{tool: "allow" for tool in sorted(READ_TOOLS)}, **fixed["actions"]},
            "shared_prompt": SYSTEM_PROMPT,
            "fixed_prompt": fixed["prompt"],
            "user_prompt": note,
            "hard_boundaries": HARD_BOUNDARIES,
            "mode_policy_version": POLICY_VERSION,
            "mode_prompt_version": digest([SYSTEM_PROMPT, fixed["prompt"], note]),
            "revision": data["revision"],
        }

    def listing(self):
        return [self.profile(mode) for mode in MODES]

    def save(self, mode, content, revision):
        validate(mode)
        if not isinstance(content, str) or len(content) > 4000:
            raise ValueError("补充指令不能超过4000字符")
        data = self.read()
        if data["revision"] != revision:
            raise ValueError("模式指令已被修改，请刷新后保存")
        data["notes"][mode] = content
        data["revision"] += 1
        atomic_json(self.path, data)
        return self.profile(mode)
