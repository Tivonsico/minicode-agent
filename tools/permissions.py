"""Small, non-configurable permission decision table.

Path and sandbox checks are separate hard boundaries. A user approval never
overrides a DENY returned by those checks or by this table.
"""

from dataclasses import dataclass
from enum import Enum


class Action(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str = ""


class PermissionPolicy:
    READ = frozenset({"ReadFile", "Glob", "Grep", "UseSkill", "ReadSkillResource"})
    MUTATE = frozenset({"WriteFile", "EditFile", "DeleteFile"})

    def decide(self, tool_name: str, *, boundary_ok: bool = True) -> Decision:
        if not boundary_ok:
            return Decision(Action.DENY, "Resource boundary denied this operation.")
        if tool_name in self.READ:
            return Decision(Action.ALLOW)
        if tool_name in self.MUTATE or tool_name == "Shell":
            return Decision(Action.ASK)
        return Decision(Action.DENY, "Unknown operation.")
