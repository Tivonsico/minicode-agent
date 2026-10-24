"""Validated tool execution with read groups and exclusive mutation barriers."""

import asyncio
import json
import sys
import time
from pathlib import Path

from backend.execution.gate import execution_gate
from backend.execution.python_runtime import python_runtime
from backend.modes.settings import ModeSettings
from backend.observability.scope import active_store, scope
from backend.permissions.paths import PathPolicy
from backend.permissions.policy import PermissionPolicy

from .base import ToolError, ToolResult
from .dispatch import ToolDispatch
from .file_operations import FileOperations
from .registry import build_registry
from .search import SearchOperations


class ToolExecutor(ToolDispatch, FileOperations, SearchOperations):
    def __init__(
        self, root: Path, output_store, approve, emit=None, session_permissions=None, skills=None,
        *, history_recall=False,
    ):
        from backend.context.project_assets import ProjectAssets
        from backend.skills import SkillCatalog

        self.root = Path(root).resolve()
        self.history_recall = history_recall
        self.output_store = output_store
        self.approve = approve
        self.emit = emit or (lambda *args, **kwargs: None)
        self.session_permissions = session_permissions
        self.skills = skills or SkillCatalog(
            self.root, lambda: session_permissions.roots() if session_permissions else ()
        )
        self.assets = ProjectAssets(self.root)
        self.policy = PathPolicy(
            self.root,
            output_store.allowed_paths,
            (lambda: self.session_permissions.roots()) if session_permissions else (lambda: ()),
            include_root=session_permissions is None,
        )
        self.permissions = PermissionPolicy()
        self.agent_mode = "manual"
        self.mode_profile = ModeSettings(self.root).profile(self.agent_mode)
        self.registry = build_registry(self._dispatch, history_recall=history_recall)
        from backend.recovery.effects import register
        register(self)
        self.shell_executable = None
        self.shell_backend = None
        self._lock = asyncio.Lock()
        self.reset_turn()

    def schemas(self):
        from backend.permissions.policy import Action

        return [
            schema
            for schema in self.registry.schemas()
            if self.permissions.decide(schema["function"]["name"], mode=self.agent_mode).action
            != Action.DENY
        ]

    def permission_context(self):
        roots = (
            "\n".join(f"- {path}" for path in self.policy.roots())
            or "(none; add a work directory in the browser)"
        )
        dialect = (
            "PowerShell"
            if self.shell_executable and "powershell" in self.shell_executable.lower()
            else "POSIX shell（标准 sh 命令；Bash/zsh/ksh 管道保留失败退出码）"
        )
        shell = (
            f"Shell policy: {self.mode_profile['actions']['Shell']}; available only in a verified Codex OS sandbox."
            if self.shell_backend is not None and self.shell_backend.available()
            else "Shell is unavailable because its system sandbox has not been verified."
        )
        runtime = python_runtime(self.root)
        environment = getattr(self.shell_backend, "python_environment", None)
        if environment:
            runtime = (
                environment,
                Path(environment)
                / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"),
            )
        python = (
            f" Python runtime: {runtime[1]}; python3/python resolve to this runtime by default."
            if runtime
            else ""
        )
        history_note = (
            "RecallHistory reads this session's original user messages without a work directory "
            "or network. For questions about earlier conversation details, use it before claiming "
            "the information was never provided or is unavailable. " if self.history_recall else ""
        )
        return (
            f"Available tools: {', '.join(s['function']['name'] for s in self.schemas())}; session history, "
            "directory grants via the browser directory controls, global and project skills via the browser skill selector, "
            "and prompts via the browser prompt editor. "
            "Current authorized work directories (active first; relative paths use active):\n"
            f"{roots}\n"
            f"Mode {self.agent_mode}; tool decisions: {json.dumps(self.mode_profile['actions'])}. "
            "Read/search within these directories is automatic. Sensitive files stay "
            "blocked for file tools. A rejected approval ends this turn. "
            f"{history_note}{shell} Command syntax: {dialect}.{python}"
        )

    def reset_turn(self):
        self.recalled_history = ""
        blocked = getattr(self, "pause_reason", None) == "termination_unknown"
        self._effect_started = set()
        self.reads = {}
        self.text_writes = {}
        self.rejected = set()
        self._last_failure = None
        self._failure_count = 0
        self.terminal_failed = blocked
        self.pause_reason = "termination_unknown" if blocked else None
        self.active_skills = set()
        self.turn_projects = set()
        self.touched_projects = set()
        self.prompted_projects = set()

    def begin_turn(self, text: str):
        if self.history_recall:
            from backend.context.history_recall import recall_for_turn
            self.recalled_history = recall_for_turn(self.output_store.store, text)
        if not self.session_permissions:
            return
        roots = self.session_permissions.roots()
        folded = text.casefold()
        mentioned = [
            root
            for root in roots
            if str(root).casefold() in folded
            or (len(root.name) >= 3 and root.name.casefold() in folded)
        ]
        self.turn_projects.update(mentioned or roots[:1])

    def current_project_roots(self):
        if not self.session_permissions:
            return ()
        selected = self.turn_projects | self.touched_projects
        return tuple(root for root in self.session_permissions.roots() if root in selected)

    def mark_prompted(self, roots):
        self.prompted_projects.update(roots)

    def _touch_project(self, path, *, writing=False):
        if not self.session_permissions:
            return
        root = self.policy._containing_root(path)
        if root is None:
            return
        self.touched_projects.add(root)
        if writing and root not in self.prompted_projects:
            raise ToolError(
                "Project instructions must be loaded before a write; retry next request.",
                "project_context",
            )

    def force_skill(self, name: str, scope="global", project_path=None):
        content = self.skills.load(name, scope, project_path)
        if scope == "project":
            self.touched_projects.add(Path(project_path).resolve())
        self.active_skills.add((scope, str(project_path or "").casefold(), name.casefold()))
        return content

    @staticmethod
    def _key(name, args):
        return name, json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def _prepare(self, call):
        name, call_id = "unknown", "unknown"
        try:
            if not isinstance(call, dict):
                raise ToolError("Tool call must be an object.")
            call_id = call.get("id", "unknown")
            if not isinstance(call_id, str) or not call_id:
                raise ToolError("Missing tool call ID.")
            function = call.get("function", {})
            name = function.get("name", "unknown")
            tool = self.registry.get(name)
            raw = function.get("arguments", "{}")
            args = json.loads(raw) if isinstance(raw, str) else raw
            error = tool.validateInput(args)
            if error:
                raise ToolError(error, "schema")
            return call_id, name, args, tool, None
        except (ToolError, json.JSONDecodeError, TypeError, AttributeError) as error:
            error_type = getattr(error, "error_type", "invalid_json")
            return (
                call_id,
                name,
                {},
                None,
                ToolResult(call_id, name, "failed", str(error), error_type),
            )

    async def execute_batch(self, calls):
        try:
            async with self.execution_gate() as lease:
                self._lease = lease
                try:
                    return await self._execute_batch(calls)
                finally:
                    self._lease = None
        except ToolError as error:
            if error.error_type != "termination_unknown":
                raise
            self.terminal_failed, self.pause_reason = True, "workspace_unknown"
            results = []
            for call in calls:
                cid, name, *_ = self._prepare(call)
                result = ToolResult(cid, name, "cancelled", str(error), "workspace_unknown")
                self.emit("tool_result", **result.to_dict())
                results.append(result)
            return results

    def execution_gate(self, *, reconciliation=False):
        store = getattr(self.output_store, "store", None)
        if getattr(store, "database", None):
            from backend.execution.mysql_gate import mysql_execution_gate

            return mysql_execution_gate(store, self.policy.roots(), reconciliation=reconciliation)
        return execution_gate(reconciliation=reconciliation)

    async def _execute_batch(self, calls):
        async with self._lock:
            prepared = [self._prepare(call) for call in calls]
            results = []
            i = 0
            while i < len(prepared):
                if self.terminal_failed:
                    for cid, name, *_ in prepared[i:]:
                        result = ToolResult(
                            cid,
                            name,
                            "cancelled",
                            "Not executed: " + str(self.pause_reason),
                            "paused",
                        )
                        self.emit("tool_result", **result.to_dict())
                        results.append(result)
                    break
                entry = prepared[i]
                if entry[3] and entry[3].isConcurrencySafe(entry[2]):
                    group = []
                    while i < len(prepared) and len(group) < 4:
                        item = prepared[i]
                        if not item[3] or not item[3].isConcurrencySafe(item[2]):
                            break
                        group.append(item)
                        i += 1
                    tasks = [asyncio.create_task(self._execute(item)) for item in group]
                    try:
                        completed = await asyncio.gather(*tasks)
                    except BaseException:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        raise
                    for item, result in zip(group, completed):
                        self._track(item, result)
                        results.append(result)
                else:
                    result = await self._execute(entry)
                    self._track(entry, result)
                    results.append(result)
                    i += 1
            return results

    def _track(self, item, result):
        if result.status == "unknown":
            self.terminal_failed, self.pause_reason = True, "termination_unknown"
        elif result.status == "succeeded":
            self._last_failure, self._failure_count = None, 0
        elif result.status == "failed":
            key = (*self._key(item[1], item[2]), result.error_type)
            self._failure_count = self._failure_count + 1 if key == self._last_failure else 1
            self._last_failure = key
            if self._failure_count >= 3:
                self.terminal_failed, self.pause_reason = True, "repeated_tool_failure"

    async def _execute(self, item):
        from backend.storage.nonblocking import portal
        await portal(getattr(self.output_store, "store", None))
        started = time.monotonic()
        result = None
        store = getattr(self.output_store, "store", active_store())
        with scope(store, call_id=item[0]):
            self.emit("tool_start", call_id=item[0], name=item[1], input=item[2])
            try:
                result = await self._execute_call(item)
                return result
            finally:
                if (
                    item[1] == "Shell"
                    and item[2].get("purpose") == "verification"
                    and (result is None or result.status not in {"succeeded", "failed", "unknown"})
                ):
                    self.emit(
                        "verification_result",
                        call_id=item[0],
                        source="execution",
                        status="inconclusive",
                        reason="verification_not_finished",
                    )
                self.emit(
                    "tool_end",
                    call_id=item[0],
                    name=item[1],
                    status=result.status
                    if result
                    else (
                        "cancelled"
                        if isinstance(sys.exc_info()[1], asyncio.CancelledError)
                        else "failed"
                    ),
                    duration_ms=round((time.monotonic() - started) * 1000, 3),
                )

    def mark_running(self, cid, name):
        store = getattr(self.output_store, "store", None)
        if getattr(store, "database", None):
            self.emit("operation_prepared", call_id=cid, name=name)
        self.emit("tool_running", call_id=cid, name=name)
        self._lease.before_effect(getattr(self.output_store, "store", None), cid)
        self._effect_started.add(cid)

    async def _execute_call(self, item):
        cid, name, args, tool, invalid = item
        self._effect_started.discard(cid)
        if invalid:
            self.emit("tool_result", **invalid.to_dict())
            return invalid
        self.emit("tool_pending", call_id=cid, name=name)
        try:
            await asyncio.sleep(0)  # Cancellation boundary before any operation.
            result = await tool.execute(cid, args)
        except asyncio.CancelledError as error:
            status = getattr(error, "tool_status", "cancelled")
            if status == "unknown":
                self.terminal_failed, self.pause_reason = True, "termination_unknown"
            self.emit(
                "tool_result",
                call_id=cid,
                name=name,
                status=status,
                content=getattr(error, "tool_content", "Cancelled before completion."),
                error_type="user_interrupted",
            )
            raise
        except ToolError as error:
            status = "unknown" if error.error_type == "termination_unknown" else "failed"
            if error.error_type in {"storage_error", "termination_unknown"}:
                self.terminal_failed, self.pause_reason = True, error.error_type
            result = ToolResult(cid, name, status, str(error), error.error_type)
        except (OSError, UnicodeError, ValueError) as error:
            result = ToolResult(
                cid,
                name,
                "unknown" if cid in self._effect_started else "failed",
                f"{type(error).__name__}: {error}",
                type(error).__name__,
            )
        self.emit("tool_result", **result.to_dict())
        if name == "S2Effect" and result.status == "succeeded":
            from backend.recovery.effects import barrier
            await barrier(self, "after_result_persist", args)
        if result.status != "unknown" and cid in self._effect_started:
            self._lease.completed()
        return result

    async def _approval(self, cid, name, args, description, *, details=None):
        from backend.permissions.policy import Action

        decision = self.permissions.decide(name, mode=self.agent_mode)
        if decision.action == Action.DENY:
            raise ToolError(decision.reason, "permission")
        if decision.action == Action.ALLOW:
            self.emit("tool_approval", call_id=cid, name=name, approved=True, source="mode_policy")
            return True
        key = self._key(name, args)
        if key in self.rejected:
            return False
        self.emit("tool_awaiting_approval", call_id=cid, name=name)
        accepted = await self.approve(
            {
                "call_id": cid,
                "name": name,
                "input": dict(args),
                "description": description,
                "details": details,
            }
        )
        self.emit("tool_approval", call_id=cid, name=name, approved=bool(accepted))
        if not accepted:
            self.rejected.add(key)
            self.terminal_failed, self.pause_reason = True, "user_rejected"
        await asyncio.sleep(0)
        return bool(accepted)
