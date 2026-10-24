"""SSE framing and OpenAI tool-call delta assembly."""

import json
import time
from collections.abc import AsyncIterator


class StreamJournal:
    """Coalesce small deltas; publish only after each chunk is durable."""

    def __init__(self, emit):
        self.emit = emit
        self.pending = None
        self.size = 0
        self.last_flush = None

    def add(self, event):
        if self.pending and (self.pending["type"], self.pending.get("request_id")) != (
            event["type"], event.get("request_id")
        ):
            self.flush()
        if self.pending is None:
            self.pending = {**event, "content": [] if event["type"] == "tool_delta" else ""}
        self.pending["content"] += event.get("content", [] if event["type"] == "tool_delta" else "")
        self.size += len(str(event.get("content", "")))
        if (self.last_flush is None or self.size >= 256
                or time.monotonic() - self.last_flush >= 0.05):
            self.flush()

    def flush(self):
        if self.pending is None:
            return
        self.emit(self.pending)
        self.pending = None
        self.size = 0
        self.last_flush = time.monotonic()


async def sse_events(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    data: list[str] = []
    async for line in lines:
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield "\n".join(data)


class StreamAccumulator:
    def __init__(self):
        self.content = ""
        self.reasoning_content = ""
        self.calls: dict[int, dict] = {}
        self.finish_reason: str | None = None
        self.usage: dict | None = None
        self.received_output = False

    def add(self, chunk: dict) -> list[dict]:
        events = []
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        choices = chunk.get("choices") or []
        for choice in choices:
            if choice.get("index", 0) != 0:
                raise ValueError("Multiple response choices are unsupported")
            delta = choice.get("delta") or {}
            for field, kind in (("content", "text"), ("reasoning_content", "reasoning")):
                value = delta.get(field)
                if value:
                    self.received_output = True
                    if not isinstance(value, str):
                        raise ValueError("Non-text delta")
                    setattr(self, field, getattr(self, field) + value)
                    events.append({"type": kind, "content": value})
            parts = delta.get("tool_calls") or []
            if parts:
                self.received_output = True
                events.append({"type": "tool_delta", "content": parts})
            for part in parts:
                index = part.get("index")
                if not isinstance(index, int) or index < 0:
                    raise ValueError("Missing tool index")
                call = self.calls.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if part.get("type", "function") != "function":
                    raise ValueError("Unsupported tool type")
                if part.get("id"):
                    if call["id"] and call["id"] != part["id"]:
                        raise ValueError("Tool identity changed during stream")
                    call["id"] = part["id"]
                for key in ("name", "arguments"):
                    value = (part.get("function") or {}).get(key)
                    if value:
                        call["function"][key] += value
            if choice.get("finish_reason") is not None:
                self.finish_reason = choice["finish_reason"]
        return events

    def tool_calls(self) -> list[dict]:
        calls = [self.calls[i] for i in sorted(self.calls)]
        ids = set()
        for call in calls:
            if not call["id"] or not call["function"]["name"] or call["id"] in ids:
                raise ValueError("Incomplete or duplicate tool identity")
            ids.add(call["id"])
            if not isinstance(json.loads(call["function"]["arguments"]), dict):
                raise TypeError("Tool arguments must be a JSON object")
        return calls
