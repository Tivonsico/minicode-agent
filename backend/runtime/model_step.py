"""Stream one model response and keep bounded recovery out of the agent loop."""

import uuid

from backend.observability.requests import recorded_request
from backend.observability.scope import scope

from .streaming import StreamJournal


class ModelStep:
    """Own partial text, durable deltas and the identity of one candidate reply."""

    def __init__(self, store, turn_id, on_event):
        self.store, self.turn_id, self.on_event = store, turn_id, on_event
        self.message_id = uuid.uuid4().hex
        self.content = ""
        self.reasoning = ""
        self.recorded = False
        self.journal = StreamJournal(self._persist_delta)

    def _persist_delta(self, event):
        kind = event["type"]
        self.store.append(
            "tool_delta" if kind == "tool_delta" else "assistant_delta",
            turn_id=self.turn_id,
            message_id=self.message_id,
            request_id=event.get("request_id"),
            kind=kind,
            content=event.get("content", ""),
        )
        self.on_event(kind, event)

    async def _delta(self, event):
        if event["type"] == "text":
            self.content += event.get("content", "")
        elif event["type"] == "reasoning":
            self.reasoning += event.get("content", "")
        self.journal.add(event)

    async def complete(self, provider, config, messages, schemas, on_attempt):
        with scope(self.store, message_id=self.message_id):
            response = await recorded_request(
                provider,
                self.store,
                messages,
                schemas,
                self._delta,
                on_attempt,
                config=config,
                flush_delta=self.journal.flush,
            )
        return response

    def save_partial(self, conversation):
        if not self.recorded:
            conversation.message(
                "assistant",
                self.content,
                message_id=self.message_id,
                reasoning_content=self.reasoning,
                status="partial",
            )
            self.recorded = True


class StreamRecovery:
    """Retry a dropped partial stream once; never dispatch its candidate tools."""

    def __init__(self, store, on_event):
        self.store, self.on_event = store, on_event
        self.count = 0
        self.feedback = ""

    def recover(self, error, step, executor):
        if (
            not error.retryable
            or not error.received_output
            or self.count >= 1
            or executor.terminal_failed
        ):
            return False
        self.count += 1
        failed = next(
            (
                event
                for event in reversed(self.store.view)
                if event["type"] == "request_end" and event.get("message_id") == step.message_id
            ),
            {},
        )
        self.store.append(
            "request_recovery",
            request_id=failed.get("request_id"),
            message_id=step.message_id,
            discarded_tool_calls=len(error.tool_calls),
            recovery=self.count,
        )
        self.feedback = (
            "上一条模型响应在流式传输中中断，其中的工具未执行。"
            "从已确认的工作区和对话状态继续，不重放此前成功执行过的工具。"
            "本次恢复会记录为新的模型请求；用户中断和已有超时设置仍生效。"
        )
        self.on_event(
            "notice", {"text": "模型流中断；未完成工具调用已丢弃，正在从已确认状态重试一次。"}
        )
        step.recorded = True
        return True
