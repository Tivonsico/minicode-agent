"""Route validated calls to handlers; output offloading stays uniform."""

from backend.permissions.policy import Action

from . import mutation_calls, read_calls, search_calls, shell_calls, skill_calls
from .base import ToolError, ToolResult

HANDLERS = {
    "UseSkill": skill_calls.execute,
    "ReadSkillResource": skill_calls.execute,
    "ReadFile": read_calls.execute,
    "Glob": search_calls.execute,
    "Grep": search_calls.execute,
    "WriteFile": mutation_calls.execute,
    "EditFile": mutation_calls.execute,
    "DeleteFile": mutation_calls.execute,
    "Shell": shell_calls.execute,
}


class ToolDispatch:
    async def _dispatch(self, name, cid, args):
        decision = self.permissions.decide(name, mode=self.agent_mode)
        self.emit(
            "permission_decision",
            layer="mode",
            call_id=cid,
            name=name,
            action=decision.action.value,
            reason=decision.reason,
        )
        if decision.action == Action.DENY:
            raise ToolError("Operation denied by permission policy.", "permission")
        content = await HANDLERS[name](self, name, cid, args)
        if isinstance(content, ToolResult):
            return content
        limit = 70000 if name in {"UseSkill", "ReadSkillResource"} else 16000
        return ToolResult(
            cid, name, "succeeded", self.output_store.preview(cid, content, limit=limit)
        )
