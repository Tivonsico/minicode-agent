import copy
import json

from backend.conversation.snapshot import discarded_message_ids

from .budget import estimate, input_budget
from .compactor import summarize
from .compation_ckeckpoint import CompactionCheckpoint


class ContextOverflow(RuntimeError):
    pass


def groups(messages):
    """Keep a tool-calling assistant and all its results indivisible."""
    result = []
    for message in messages:
        if message.get("role") == "tool" and result:
            result[-1].append(message)
        else:
            result.append([message])
    return result


def valid_tools(messages):
    pending = set()
    for message in messages:
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                return False
            pending.remove(call_id)
        else:
            if pending:
                return False
            pending = {c["id"] for c in message.get("tool_calls", [])}
    return not pending


class ContextManager:
    def __init__(self, config, provider, store):
        self.config, self.provider, self.store = config, provider, store
        self.last_estimate = 0
        self._source_body, self._compressed_body = [], []
        self.compaction_checkpoint = CompactionCheckpoint(store)

    async def measure(self, messages, tools):
        counter = getattr(self.provider, "count_tokens", None)
        count = await counter(messages, tools) if counter else None
        byte_bound = estimate(messages, tools)
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError("Invalid provider token count")
        self.store.append(
            "context_measurement",
            method="provider_tokenizer" if count is not None else "utf8_byte_upper_bound",
            token_count=count,
            byte_upper_bound=byte_bound,
            input_budget=input_budget(self.config),
            trigger_ratio=0.8,
        )
        return byte_bound if count is None else count

    async def prepare(self, messages, tools):
        original = copy.deepcopy(messages)
        system = [m for m in original if m.get("role") in ("system", "developer")]
        body = [m for m in original if m.get("role") not in ("system", "developer")]
        raw_body = copy.deepcopy(body)
        if not self._source_body:
            restored = self.compaction_checkpoint.restore(body)
            if restored and valid_tools(restored[1]):
                self._source_body, self._compressed_body = restored
        if self._source_body and body[: len(self._source_body)] == self._source_body:
            body = copy.deepcopy(self._compressed_body) + body[len(self._source_body) :]
        current = system + body
        budget = input_budget(self.config)
        self.last_estimate = await self.measure(current, tools)
        if self.last_estimate < 0.8 * budget:
            return current
        grouped = groups(body)
        latest_user = max(
            (i for i, g in enumerate(grouped) if g[0].get("role") == "user"), default=0
        )
        selected = {i for i in range(max(0, len(grouped) - 4)) if i != latest_user}
        old = [m for i, g in enumerate(grouped) if i in selected for m in g]
        recent = [m for i, g in enumerate(grouped) if i not in selected for m in g]
        if old:
            try:
                summary = await summarize(
                    self.provider, self.store, old, budget
                )
                candidate = system + [{"role": "user", "content": summary}] + recent
                candidate_size = await self.measure(candidate, tools)
                if (
                    candidate_size <= budget
                    and candidate_size < self.last_estimate
                    and valid_tools(candidate)
                ):
                    self._source_body = raw_body
                    self._compressed_body = copy.deepcopy(candidate[len(system) :])
                    self.last_estimate = candidate_size
                    self.compaction_checkpoint.save(raw_body, self._compressed_body, candidate_size)
                    return candidate
            except Exception:
                self.store.append(
                    "context_compression_error",
                    reason="Compression failed; original context retained",
                )
        if self.last_estimate > budget:
            self.store.append(
                "context_overflow", input_estimate=self.last_estimate, input_budget=budget
            )
            raise ContextOverflow(
                "Context exceeds input budget; history saved. Compression could not safely reduce it."
            )
        return current


def project_messages(events: list[dict]) -> list[dict]:
    reconciled = {e["result_seq"]: e for e in events if e["type"] == "operation_reconciled"}
    messages = []
    seen = set()
    pending = {}
    discarded = discarded_message_ids(events)

    def flush_pending():
        for call_id in list(pending):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(
                        {
                            "status": "unknown",
                            "message": "[UNKNOWN] 执行中断或结果缺失；不得自动重跑，请先检查现状。",
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        pending.clear()

    for event in events:
        if event["type"] == "run_accepted" and isinstance(event.get("input"), dict):
            event = {**event, **event["input"], "type": "message"}
        if event["type"] == "message":
            message_id = event["message_id"]
            if message_id in discarded:
                continue
            if message_id in seen:
                continue
            seen.add(message_id)
            flush_pending()
            message = {
                key: event[key]
                for key in ("role", "content", "tool_calls", "reasoning_content")
                if key in event
            }
            if event.get("status") == "partial":
                message.pop("tool_calls", None)
                message["content"] = message.get("content", "") + "\n[上次回复未完整结束]"
            messages.append(message)
            for call in message.get("tool_calls", []):
                pending[call["id"]] = (event.get("turn_id"), event.get("request_id"))
        elif event["type"] == "tool_result":
            if event.get("seq") in reconciled:
                record = reconciled[event["seq"]]
                event = {
                    **event,
                    "content": json.dumps(
                        {
                            "status": record["resolution"],
                            "source": "manual_reconciliation",
                            "evidence": record["evidence"],
                            "automatic_retry": False,
                        },
                        ensure_ascii=False,
                    ),
                }
            elif event.get("status") in {"unknown", "cancelled", "interrupted", "failed", "rejected"}:
                status = "unknown" if event["status"] == "interrupted" else event["status"]
                event = {**event, "content": json.dumps({
                    "status": status,
                    "message": ("[UNKNOWN] " if status == "unknown" else "") + event.get("content", ""),
                    "automatic_retry": False,
                }, ensure_ascii=False)}
            call_id = event.get("call_id", event.get("tool_call_id"))
            if call_id in pending and pending[call_id] == (event.get("turn_id"), event.get("request_id")):
                messages.append(
                    {"role": "tool", "tool_call_id": call_id, "content": event.get("content", "")}
                )
                pending.pop(call_id)
    flush_pending()
    return messages
