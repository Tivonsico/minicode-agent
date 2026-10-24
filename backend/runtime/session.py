"""Own one session's run lifecycle; clients only observe or issue commands."""

import asyncio
import copy
import uuid

from backend.context import ContextManager
from backend.context.budget import input_budget
from backend.conversation.operations import unresolved_operations
from backend.conversation.resume import recover_session
from backend.conversation.store import SessionStore
from backend.execution.codex import CodexSandbox
from backend.long_memory.scoped import ScopedMemory
from backend.models.reasoning import validate as validate_reasoning
from backend.modes.catalog import tags
from backend.modes.catalog import validate as validate_mode
from backend.modes.settings import ModeSettings
from backend.observability.scope import scope
from backend.permissions.directories import SessionPermissions
from backend.snapshots.session import SessionSnapshots
from backend.storage.output_store import OutputStore
from backend.tools.executor import ToolExecutor

from .approvals import ApprovalBroker
from .loop import AgentLoop


class RuntimeSession:
    def __init__(self, config, session_id, provider, long_memory, sandbox, *, store=None):
        self.config = config
        self.store = store or SessionStore(config.root, session_id, secrets=(config.api_key,))
        recover_session(self.store)
        self.changed = asyncio.Event()
        self.permissions = SessionPermissions(config.root, self.store)
        self.approvals = ApprovalBroker(self.publish, self.store)
        self.output = OutputStore(self.store, secrets=self.store.secrets)
        self.executor = ToolExecutor(
            config.root,
            self.output,
            self.approvals.ask,
            self.publish,
            session_permissions=self.permissions,
            history_recall=config.experimental_history_recall,
        )
        if unresolved_operations(self.store.view):
            self.executor.terminal_failed = True
            self.executor.pause_reason = "termination_unknown"
        self.executor.shell_executable = config.shell
        self.executor.shell_backend = CodexSandbox(
            config.root, self.permissions.roots, sandbox.executable
        )
        self.executor.shell_backend._verified = sandbox.available()
        self.executor.shell_backend.reason = sandbox.reason
        self.context = ContextManager(config, provider, self.store)
        self.memory = ScopedMemory(long_memory, self.permissions)
        self.selected_skill = ""
        self.loop = AgentLoop(
            config,
            self.store,
            provider,
            self.executor,
            self.context,
            self.memory,
            on_event=self.observe,
            skill_context=lambda: self.selected_skill,
        )
        self._starting = False
        self.task = None
        self.run_id = None
        self.turn_id = None
        self.agent_mode = next(
            (e["agent_mode"] for e in reversed(self.store.view) if e["type"] == "mode_selected"),
            "manual",
        )
        self.mode_settings = ModeSettings(config.root)
        self.run_profile = None
        self.file_snapshots = SessionSnapshots(self)

    def publish(self, kind, **data):
        event = self.store.append(kind, **data)
        self.changed.set()
        return event

    def observe(self, kind, data):
        # Text/tool deltas are already durable in the loop; wake SSE without duplication.
        if kind in {"text", "reasoning", "tool_delta"}:
            self.changed.set()
        else:
            self.publish(kind, **data)

    def busy(self):
        return (self._starting or getattr(self, "_restoring_files", False)
                or getattr(self, "_recovering_operations", False)
                or (self.task is not None and not self.task.done()))

    def select_mode(self, mode):
        validate_mode(mode)
        if self.busy():
            raise ValueError("执行中不能切换模式，请先停止或等待结束")
        if mode != self.agent_mode:
            self.agent_mode = mode
            self.publish("mode_selected", agent_mode=mode)

    def start(self, text, reasoning_effort=None, agent_mode=None, *, mode_profile=None):
        if self.store.failed:
            raise ValueError("会话存储状态未确认，请重新打开会话后再执行")
        reasoning_effort = validate_reasoning(self.config, reasoning_effort)
        if self.executor.terminal_failed and self.executor.pause_reason == "termination_unknown":
            raise ValueError("此会话包含终止状态未确认的操作；请人工核对实际状态，不能自动继续")
        if self.busy():
            raise ValueError("当前会话仍在执行，请先停止或等待结束")
        if agent_mode is not None:
            self.select_mode(agent_mode)
        profile = copy.deepcopy(mode_profile or self.mode_settings.profile(self.agent_mode))
        if profile["agent_mode"] != self.agent_mode:
            raise ValueError("冻结模式与会话模式不一致")
        self.run_profile = profile
        self.executor.agent_mode = self.agent_mode
        self.executor.mode_profile = profile
        self.run_id = uuid.uuid4().hex
        self.turn_id = uuid.uuid4().hex
        self.file_snapshots.entered = False
        self._starting = True
        try:
            self.publish(
                "run_accepted",
                run_id=self.run_id,
                turn_id=self.turn_id,
                reasoning_effort=reasoning_effort,
                input={"message_id": uuid.uuid4().hex, "role": "user", "content": text},
                mode_profile=profile,
                **tags(profile),
            )
        except asyncio.CancelledError:
            # The accept transaction may have committed while its HTTP command
            # was cancelled. Close the accepted lifecycle without dispatching.
            self.publish("run_end", run_id=self.run_id, turn_id=self.turn_id,
                         status="interrupted", reason="cancelled_before_dispatch")
            raise
        finally:
            self._starting = False
        self.approvals.run_id = self.run_id
        self.task = asyncio.create_task(
            self._run_scoped(text, self.run_id, self.turn_id, reasoning_effort, profile)
        )
        return self.run_id

    async def _run_scoped(self, text, run_id, turn_id, reasoning_effort, profile):
        from backend.storage.nonblocking import portal
        await portal(self.store)
        with scope(
            self.store,
            run_id=run_id,
            turn_id=turn_id,
            reasoning_effort=reasoning_effort,
            **tags(profile),
        ):
            await self._run(text)

    async def _run(self, text):
        try:
            await self.file_snapshots.run(lambda: self.loop.run(text))
        except Exception as exc:
            self.loop.status = "failed"
            # Do not expose provider credentials or arbitrary exception content to a client.
            try:
                if self.loop.conversation.active:
                    self.loop._finish("failed", "runtime_error")
                else:
                    self.publish("run_end", run_id=self.run_id, turn_id=self.turn_id,
                                 status="failed", reason="snapshot_or_runtime_error")
                self.publish(
                    "notice", text=f"执行失败：{type(exc).__name__}；请检查本地状态后继续。"
                )
            except Exception:
                pass
        finally:
            self.selected_skill = ""
            self.publish("file_snapshots_ready", run_id=self.run_id, turn_id=self.turn_id)
            self.changed.set()

    async def cancel(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                if not self.file_snapshots.entered:
                    self.loop.status = "interrupted"
                    async def no_effect():
                        pass
                    await self.file_snapshots.run(no_effect)
                # Cancellation before the coroutine starts has no dispatched side effect.
                self.publish(
                    "run_end",
                    run_id=self.run_id,
                    turn_id=self.turn_id,
                    status="interrupted",
                    reason="cancelled_before_start",
                    **tags(self.run_profile or {}),
                )
                self.publish("file_snapshots_ready", run_id=self.run_id, turn_id=self.turn_id)
                self.changed.set()

    def detail(self):
        return {
            "session": self.store.meta,
            "messages": self.loop.conversation.messages(),
            "events": self.store.view,
            "recovery": self.store.recovery_info,
            "unresolved_operations": unresolved_operations(self.store.view),
            "running": self.busy(),
            "run_id": self.run_id,
            "agent_mode": self.agent_mode,
            "mode_profile": self.run_profile
            if self.busy()
            else self.mode_settings.profile(self.agent_mode),
            "approvals": self.approvals.listing(),
            "directories": self.permissions.listing(),
            "context_budget": input_budget(self.config),
            "model_capabilities": {
                "context_window": self.config.context_window,
                "max_output": self.config.model_max_output,
                "source": self.config.capability_source,
            },
        }
