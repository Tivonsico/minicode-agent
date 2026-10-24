from __future__ import annotations

import asyncio
import sys
import time
import uuid

from backend.context import ContextOverflow
from backend.context.prompts import assemble_system_prompt
from backend.conversation.manager import ConversationManager
from backend.modes.catalog import tags
from backend.observability.scope import current, scope
from backend.runtime.provider import ProviderError

from .model_step import ModelStep, StreamRecovery
from .verification import CompletionReview


class AgentLoop:
    def __init__(
        self,
        config,
        store,
        provider,
        executor,
        context,
        long_memory,
        on_event=None,
        skill_context=None,
    ):
        self.config, self.store, self.provider = config, store, provider
        self.executor, self.context, self.long_memory = executor, context, long_memory
        self.conversation = ConversationManager(store)
        self.on_event = on_event or (lambda kind, data: None)
        self.skill_context = skill_context or (lambda: "")
        self.requests = 0
        self.status = "idle"

    async def run(self, text: str):
        identities = current(self.store)
        with scope(
            self.store,
            run_id=identities.get("run_id") or uuid.uuid4().hex,
            turn_id=identities.get("turn_id") or uuid.uuid4().hex,
            step_id=None,
            request_id=None,
            call_id=None,
            approval_id=None,
        ):
            self.store.append("run_start")
            try:
                result = await self._run(text)
                self.status = result
                return result
            except BaseException as error:
                self.status = (
                    "interrupted" if isinstance(error, asyncio.CancelledError) else "failed"
                )
                raise
            finally:
                self.store.append("run_end", status=self.status)

    async def _run(self, text: str):
        if self.executor.terminal_failed and self.executor.pause_reason in (
            "termination_unknown",
            "storage_error",
        ):
            self.on_event("notice", {"text": "上一操作状态尚未确认或存储不可用，不能继续执行。"})
            return "paused"
        user = self.conversation.start(text)
        self.executor.reset_turn()
        if hasattr(self.executor, "begin_turn"):
            self.executor.begin_turn(text)
        self.requests = 0
        self.status = "running"
        model_step = None
        review = CompletionReview(self.store, self.conversation.turn_id)
        recovery = StreamRecovery(self.store, self.on_event)
        try:
            while True:
                # Explicit cancellation boundary before any new provider or tool dispatch.
                await asyncio.sleep(0)
                step_id = uuid.uuid4().hex
                with scope(self.store, step_id=step_id):
                    step_started = time.monotonic()
                    step_recovered = False
                    self.store.append("step_start")
                    try:
                        system = assemble_system_prompt(
                            self.executor, self.long_memory, self.skill_context()
                        )
                        if review.feedback:
                            system += "\n\n" + review.feedback
                        if recovery.feedback:
                            system += "\n\n" + recovery.feedback
                        messages = [
                            {"role": "system", "content": system}
                        ] + self.conversation.messages()
                        schemas = self.executor.schemas()
                        messages = await self.context.prepare(messages, schemas)
                        self.on_event("context", {"estimated_tokens": self.context.last_estimate})
                        model_step = ModelStep(self.store, self.conversation.turn_id, self.on_event)
                        message_id = model_step.message_id

                        async def attempt(number):
                            self.requests += 1

                        try:
                            response = await model_step.complete(
                                self.provider,
                                self.config,
                                messages,
                                schemas,
                                attempt,
                            )
                        except ProviderError as error:
                            with scope(self.store, message_id=message_id):
                                if not recovery.recover(
                                    error, model_step, self.executor
                                ):
                                    raise
                            step_recovered = True
                            continue
                        recovery.feedback = ""
                        request_id = response.request_id

                        calls = response.tool_calls
                        normal = response.finish_reason == "stop" and not calls
                        valid_tools = bool(calls) and response.finish_reason in (
                            "tool_calls",
                            "stop",
                        )
                        blocked = False
                        backend = getattr(self.executor, "shell_backend", None)
                        if normal and backend is not None and backend.available():
                            blocked = review.evaluate(request_id, message_id)
                        fields = {
                            "message_id": message_id,
                            "request_id": request_id,
                            "is_final": normal and not blocked,
                            "finish_reason": response.finish_reason,
                            "status": "complete" if normal or valid_tools else "partial",
                        }
                        if valid_tools:
                            fields["tool_calls"] = calls
                        if response.reasoning_content:
                            fields["reasoning_content"] = response.reasoning_content
                        if blocked:
                            # Preserve the candidate for audit, not as another chat reply.
                            self.store.append(
                                "completion_deferred", content=response.content, **fields
                            )
                            model_step.recorded = True
                            if review.exhausted:
                                self.conversation.message(
                                    "assistant",
                                    "修改已保存，但结束前验证仍未通过；现场已保留。"
                                    "请检查验证条件或环境后继续。",
                                    **{**fields, "message_id": uuid.uuid4().hex},
                                )
                                self._finish("paused", "verification_required")
                                self.on_event(
                                    "notice",
                                    {"text": "修改已保存，但结束前验证仍未通过；现场已保留。"},
                                )
                                return "paused"
                            continue
                        self.conversation.message("assistant", response.content, **fields)
                        model_step.recorded = True
                        if normal:
                            self._finish("completed", "stop")
                            self.long_memory.schedule(
                                self.store,
                                self.conversation.turn_id,
                                [
                                    {
                                        "id": user["message_id"],
                                        "content": text,
                                        "time": user["time"],
                                        "source_run": current(self.store).get("run_id"),
                                        "source_turn": self.conversation.turn_id,
                                        **tags(current(self.store)),
                                    }
                                ],
                            )
                            return "completed"
                        if not valid_tools:
                            self._finish("paused", "model_" + str(response.finish_reason))
                            self.on_event(
                                "notice", {"text": "模型未正常完成，现场已保存；请检查后继续。"}
                            )
                            return "paused"
                        await asyncio.sleep(0)
                        with scope(self.store, message_id=message_id, request_id=request_id):
                            results = await self.executor.execute_batch(calls)
                        for result in results:
                            # The executor records final results, including cancellation, at the point of completion.
                            if not any(
                                e["type"] == "tool_result"
                                and e.get("call_id") == result.call_id
                                and e.get("request_id") == request_id
                                and e.get("turn_id") == self.conversation.turn_id
                                for e in self.store.view
                            ):
                                self.store.append(
                                    "tool_result",
                                    turn_id=self.conversation.turn_id,
                                    request_id=request_id,
                                    **result.to_dict(),
                                )
                        if self.executor.terminal_failed:
                            self._finish("paused", self.executor.pause_reason)
                            self.on_event(
                                "notice",
                                {
                                    "text": "共享执行层存在未核实的操作，请先在原会话核实。"
                                    if self.executor.pause_reason == "workspace_unknown"
                                    else self.executor.pause_reason
                                },
                            )
                            return "paused"
                    finally:
                        error = sys.exc_info()[1]
                        self.store.append(
                            "step_end",
                            duration_ms=round((time.monotonic() - step_started) * 1000, 3),
                            status=(
                                "cancelled"
                                if isinstance(error, asyncio.CancelledError)
                                else "failed"
                                if error or step_recovered
                                else "completed"
                            ),
                        )
        except asyncio.CancelledError:
            if model_step is not None:
                model_step.save_partial(self.conversation)
            self._finish("interrupted", "user_cancelled")
            self.on_event("notice", {"text": "已中断并保存；已发生的文件修改不会撤销。"})
            return "interrupted"
        except (ProviderError, ContextOverflow) as exc:
            if model_step is not None:
                model_step.save_partial(self.conversation)
            status = (
                "failed" if isinstance(exc, ProviderError) and not exc.received_output else "paused"
            )
            self._finish(status, type(exc).__name__)
            self.on_event("notice", {"text": str(exc)})
            return status
        except Exception:
            # Storage failures cannot be treated as a successful saved turn.
            self.status = "failed"
            self._finish("failed", "runtime_error")
            raise

    def _finish(self, status, reason):
        self.conversation.finish(status, reason)
        self.status = status
