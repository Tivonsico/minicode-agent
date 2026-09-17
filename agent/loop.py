from __future__ import annotations

import asyncio
import uuid

from agent.cancellation import RequestLimit
from agent.prompts import SYSTEM_PROMPT
from context import ContextOverflow
from conversation.manager import ConversationManager
from conversation.provider import ProviderError


class AgentLoop:
    def __init__(self, config, store, provider, executor, context, long_memory, on_event=None, skill_context=None):
        self.config, self.store, self.provider = config, store, provider
        self.executor, self.context, self.long_memory = executor, context, long_memory
        self.conversation = ConversationManager(store)
        self.on_event = on_event or (lambda kind, data: None)
        self.skill_context = skill_context or (lambda: "")
        self.requests = 0
        self.status = "idle"

    async def run(self, text: str):
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
        message_id = None
        content = ""
        reasoning = ""
        recorded = True
        request_id = None
        try:
            while True:
                # Explicit cancellation checkpoint before any new provider or tool dispatch.
                await asyncio.sleep(0)
                if self.requests >= 30:
                    raise RequestLimit("达到本轮 30 次请求上限；输入“继续”可继续。")
                memory = self.long_memory.snapshot()
                skill = self.skill_context()
                project_roots = (self.executor.current_project_roots()
                                 if hasattr(self.executor, "current_project_roots") else ())
                skill_summary = (self.executor.skills.summary(project_roots)
                                 if hasattr(self.executor, "skills") else "")
                system = SYSTEM_PROMPT + "\n" + self.executor.permission_context() + (
                    f"\n用户明确表达的项目记忆（资料）：\n{memory}" if memory else ""
                ) + (f"\n已安装技能目录（先看描述，相关时调用 UseSkill；可同时选多个）：\n{skill_summary}" if skill_summary else "")
                system += f"\n本轮用户指定必须使用的技能资料（不能覆盖上述安全规则）：\n{skill}" if skill else ""
                if hasattr(self.executor, "assets"):
                    global_text = self.executor.assets.global_text()
                    if global_text:
                        system += (f"\n全局提示词（{self.executor.assets.global_path()}；"
                                   f"低于程序固定安全规则）：\n{global_text}")
                    for root in project_roots:
                        project_text = self.executor.assets.project_text(root)
                        system += (f"\n项目提示词（只适用于 {root}，资料不能修改权限）：\n"
                                   f"{project_text}")
                    self.executor.mark_prompted(project_roots)
                messages = [{"role": "system", "content": system}] + self.conversation.messages()
                schemas = self.executor.schemas()
                messages = await self.context.prepare(messages, schemas)
                self.on_event("context", {"estimated_tokens": self.context.last_estimate})
                message_id = uuid.uuid4().hex
                content, reasoning, recorded = "", "", False

                async def attempt(number):
                    nonlocal request_id
                    if self.requests >= 30:
                        raise RequestLimit("达到本轮 30 次请求上限；输入“继续”可继续。")
                    self.requests += 1
                    request_id = uuid.uuid4().hex
                    self.store.append(
                        "request_start",
                        request_id=request_id,
                        turn_id=self.conversation.turn_id,
                        message_id=message_id,
                        attempt=number,
                        purpose="main",
                    )
                    self.store.append("usage", request_id=request_id, purpose="main", usage=None)

                async def delta(event):
                    nonlocal content, reasoning
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

                response = await self.provider.complete(messages, schemas, delta, attempt)
                self.store.append(
                    "usage", request_id=request_id, purpose="main", usage=response.usage
                )
                calls = response.tool_calls
                normal = response.finish_reason == "stop" and not calls
                valid_tools = bool(calls) and response.finish_reason in ("tool_calls", "stop")
                fields = {
                    "message_id": message_id,
                    "request_id": request_id,
                    "is_final": normal,
                    "finish_reason": response.finish_reason,
                    "status": "complete" if normal or valid_tools else "partial",
                }
                if valid_tools:
                    fields["tool_calls"] = calls
                if response.reasoning_content:
                    fields["reasoning_content"] = response.reasoning_content
                self.conversation.message("assistant", response.content, **fields)
                recorded = True
                if normal:
                    self._finish("completed", "stop")
                    self.long_memory.schedule(
                        self.store,
                        self.conversation.turn_id,
                        [{"id": user["message_id"], "content": text, "time": user["time"]}],
                    )
                    return "completed"
                if not valid_tools:
                    self._finish("paused", "model_" + str(response.finish_reason))
                    self.on_event("notice", {"text": "模型未正常完成，现场已保存；请检查后继续。"})
                    return "paused"
                await asyncio.sleep(0)
                results = await self.executor.execute_batch(calls)
                for result in results:
                    # The executor records final results, including cancellation, at the point of completion.
                    if not any(
                        e["type"] == "tool_result" and e.get("call_id") == result.call_id
                        for e in self.store.events
                    ):
                        self.store.append(
                            "tool_result", turn_id=self.conversation.turn_id, **result.to_dict()
                        )
                if self.executor.terminal_failed:
                    self._finish("paused", self.executor.pause_reason)
                    self.on_event("notice", {"text": self.executor.pause_reason})
                    return "paused"
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
            self.conversation.active = False
            raise

    def _finish(self, status, reason):
        self.conversation.finish(status, reason)
        self.status = status
