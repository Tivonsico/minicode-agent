from __future__ import annotations

import asyncio
import sys
import time
import uuid

from backend.context import ContextOverflow
from backend.context.prompts import assemble_system_prompt
from backend.conversation.manager import ConversationManager
from backend.modes.catalog import tags
from backend.observability.requests import recorded_request
from backend.observability.scope import current, scope
from backend.runtime.cancellation import RequestLimit
from backend.runtime.provider import ProviderError

from .verification import CONTRACT_REVIEW, MAX_RECHECKS, completion_check, feedback


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
        checkpoints=None,
    ):
        self.config, self.store, self.provider = config, store, provider
        self.executor, self.context, self.long_memory = executor, context, long_memory
        self.conversation = ConversationManager(store)
        self.on_event = on_event or (lambda kind, data: None)
        self.skill_context = skill_context or (lambda: "")
        self.checkpoints = checkpoints
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
                if self.checkpoints:
                    self.checkpoints.capture()

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
        if self.checkpoints:
            self.checkpoints.begin(user["content"], self.conversation.turn_id)
        message_id = None
        content = ""
        reasoning = ""
        recorded = True
        request_id = None
        verification_feedback = ""
        rechecks = 0
        review_requested = False
        transport_recoveries = 0
        transport_feedback = ""
        try:
            while True:
                # Explicit cancellation checkpoint before any new provider or tool dispatch.
                await asyncio.sleep(0)
                if self.requests >= 30:
                    raise RequestLimit("达到本轮 30 次请求上限；输入“继续”可继续。")
                step_id = uuid.uuid4().hex
                with scope(self.store, step_id=step_id):
                    step_started = time.monotonic()
                    step_recovered = False
                    self.store.append("step_start")
                    if self.checkpoints:
                        self.checkpoints.update(phase="context", step_id=step_id)
                    try:
                        system = assemble_system_prompt(
                            self.executor, self.long_memory, self.skill_context()
                        )
                        if verification_feedback:
                            system += "\n\n" + verification_feedback
                        if transport_feedback:
                            system += "\n\n" + transport_feedback
                        if self.requests >= 24:
                            system += (
                                f"\n\n本轮剩余模型请求预算 {30 - self.requests} 次。"
                                "优先收敛已有实现、目标用例和必要回归，避免无关扩展或重复自测；"
                                "不能跳过验证或把尚未解决的问题声称为已完成。"
                            )
                        messages = [
                            {"role": "system", "content": system}
                        ] + self.conversation.messages()
                        schemas = self.executor.schemas()
                        messages = await self.context.prepare(messages, schemas)
                        if self.checkpoints:
                            self.checkpoints.update(context_estimate=self.context.last_estimate)
                        self.on_event("context", {"estimated_tokens": self.context.last_estimate})
                        message_id = uuid.uuid4().hex
                        content, reasoning, recorded = "", "", False

                        async def attempt(number):
                            if self.requests >= 30:
                                raise RequestLimit("达到本轮 30 次请求上限；输入“继续”可继续。")
                            self.requests += 1
                            if self.checkpoints:
                                self.checkpoints.update(request_count=self.requests)

                        async def delta(event):
                            nonlocal content, reasoning, request_id
                            request_id = event.get("request_id", request_id)
                            kind = event["type"]
                            part = event.get("content", "")
                            if kind == "text":
                                content += part
                            elif kind == "reasoning":
                                reasoning += part
                            self.store.append(
                                "tool_delta" if kind == "tool_delta" else "assistant_delta",
                                turn_id=self.conversation.turn_id,
                                message_id=message_id,
                                request_id=request_id,
                                kind=kind,
                                content=part,
                            )
                            self.on_event(kind, event)

                        if self.checkpoints:
                            self.checkpoints.update(phase="model")
                        with scope(self.store, message_id=message_id):
                            try:
                                response = await recorded_request(
                                    self.provider,
                                    self.store,
                                    messages,
                                    schemas,
                                    delta,
                                    attempt,
                                    config=self.config,
                                )
                            except ProviderError as error:
                                if (
                                    not error.retryable
                                    or not error.received_output
                                    or transport_recoveries >= 1
                                    or self.requests >= 30
                                    or self.executor.terminal_failed
                                ):
                                    raise
                                # Tools dispatch only after a complete response.
                                # Keep failed deltas in the journal, not model history.
                                transport_recoveries += 1
                                failed = next(
                                    (
                                        e
                                        for e in reversed(self.store.view)
                                        if e["type"] == "request_end"
                                        and e.get("step_id") == step_id
                                    ),
                                    {},
                                )
                                self.store.append(
                                    "request_recovery",
                                    request_id=failed.get("request_id"),
                                    message_id=message_id,
                                    discarded_tool_calls=len(error.tool_calls),
                                    recovery=transport_recoveries,
                                )
                                transport_feedback = (
                                    "上一条模型响应在流式传输中中断，其中的工具未执行。"
                                    "从已确认的工作区和对话状态继续，不重放此前成功执行过的工具。"
                                    "本次恢复仍计入原有请求和时间预算。"
                                )
                                self.on_event(
                                    "notice",
                                    {
                                        "text": "模型流中断；未完成工具调用已丢弃，正在从已确认状态重试一次。"
                                    },
                                )
                                recorded = True
                                step_recovered = True
                                continue
                        transport_feedback = ""
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
                            check = completion_check(self.store.view, self.conversation.turn_id)
                            if check["required"]:
                                review = check["accepted"] and not review_requested
                                self.store.append(
                                    "completion_check",
                                    request_id=request_id,
                                    message_id=message_id,
                                    status="review_requested"
                                    if review
                                    else "accepted"
                                    if check["accepted"]
                                    else "blocked",
                                    review_requested=review,
                                    **check,
                                )
                                blocked = not check["accepted"] or review
                                if review:
                                    review_requested = True
                                    verification_feedback = CONTRACT_REVIEW
                                else:
                                    verification_feedback = "" if not blocked else feedback(check)
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
                        self.conversation.message("assistant", response.content, **fields)
                        recorded = True
                        if blocked:
                            if check["accepted"] is False:
                                rechecks += 1
                            if rechecks > MAX_RECHECKS:
                                self._finish("paused", "verification_required")
                                self.on_event(
                                    "notice",
                                    {"text": "修改已保存，但结束前验证仍未通过；现场已保留。"},
                                )
                                return "paused"
                            if self.checkpoints:
                                self.checkpoints.update(phase="verification")
                            continue
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
                        if self.checkpoints:
                            self.checkpoints.update(phase="tools")
                        with scope(self.store, message_id=message_id, request_id=request_id):
                            results = await self.executor.execute_batch(calls)
                        for result in results:
                            # The executor records final results, including cancellation, at the point of completion.
                            if not any(
                                e["type"] == "tool_result" and e.get("call_id") == result.call_id
                                for e in self.store.view
                            ):
                                self.store.append(
                                    "tool_result",
                                    turn_id=self.conversation.turn_id,
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
                        if self.checkpoints:
                            self.checkpoints.capture()
        except asyncio.CancelledError:
            if not recorded and message_id:
                self.conversation.message(
                    "assistant",
                    content,
                    message_id=message_id,
                    reasoning_content=reasoning,
                    status="partial",
                )
            self._finish("interrupted", "user_cancelled")
            self.on_event("notice", {"text": "已中断并保存；已发生的文件修改不会撤销。"})
            return "interrupted"
        except (ProviderError, ContextOverflow, RequestLimit) as exc:
            if not recorded and message_id:
                self.conversation.message(
                    "assistant",
                    content,
                    message_id=message_id,
                    reasoning_content=reasoning,
                    status="partial",
                )
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
        if self.checkpoints:
            self.checkpoints.update(status=status, pending_tool=None, phase="ended")
