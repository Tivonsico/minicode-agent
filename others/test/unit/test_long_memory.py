import asyncio
import json
from types import SimpleNamespace

import pytest

from memory.long_memory import LongMemory


class Store:
    session_id = "session"

    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append({"type": event_type, **data})


class Provider:
    def __init__(self):
        self.gates = {}
        self.fail = False

    async def complete(self, messages, tools, **kwargs):
        source = json.loads(messages[-1]["content"])[0]
        if source["id"] in self.gates:
            await self.gates[source["id"]].wait()
        if self.fail:
            raise RuntimeError("API key must never be logged")
        return SimpleNamespace(
            content=json.dumps(
                {
                    "entries": [
                        {"key": "language", "source_id": source["id"], "quote": source["content"]},
                        {
                            "key": "invented",
                            "source_id": source["id"],
                            "quote": "User is an expert",
                        },
                    ]
                }
            ),
            finish_reason="stop",
            tool_calls=[],
            usage={"total_tokens": 10},
        )


def source(id, content, time):
    return [{"id": id, "content": content, "time": time}]


@pytest.mark.asyncio
async def test_background_merge_uses_latest_statement_not_completion_order(tmp_path):
    provider, store = Provider(), Store()
    provider.gates["old"] = asyncio.Event()
    memory = LongMemory(tmp_path, provider)
    old = memory.schedule(store, "old", source("old", "Use Java", "2025-01-01"))
    await asyncio.sleep(0)
    assert not old.done()
    await memory.schedule(store, "new", source("new", "Use Python", "2026-01-01"))
    provider.gates["old"].set()
    await old
    assert len(memory.entries()) == 1
    assert memory.entries()[0]["content"] == "Use Python"
    assert memory.entries()[0]["source_message"] == "new"
    (tmp_path / "other").mkdir()
    assert LongMemory(tmp_path / "other", provider).entries() == []
    assert LongMemory(tmp_path, provider).entries() == memory.entries()


@pytest.mark.asyncio
async def test_deleted_memory_not_resurrected_by_pending_task(tmp_path):
    provider, store = Provider(), Store()
    memory = LongMemory(tmp_path, provider)
    await memory.schedule(store, "first", source("first", "Use Python", "2025"))
    provider.gates["pending"] = asyncio.Event()
    task = memory.schedule(store, "pending", source("pending", "Use Go", "2026"))
    await asyncio.sleep(0)
    assert memory.delete(memory.entries()[0]["id"])
    provider.gates["pending"].set()
    await task
    assert memory.entries() == []
    assert memory.schedule(store, "first", source("first", "Use Python", "2025")) is None


@pytest.mark.asyncio
async def test_failure_and_close_preserve_history_and_cancel_tasks(tmp_path):
    provider, store = Provider(), Store()
    provider.fail = True
    memory = LongMemory(tmp_path, provider)
    await memory.schedule(store, "fail", source("fail", "Use Python", "2025"))
    assert any(e["type"] == "memory_error" for e in store.events)
    assert "API key" not in str(store.events)
    provider.gates["slow"] = asyncio.Event()
    task = memory.schedule(store, "slow", source("slow", "Use Python", "2026"))
    await asyncio.sleep(0)
    assert memory.schedule(store, "slow", source("slow", "Use Python", "2026")) is task
    await memory.close()
    assert task.cancelled()
    assert any(e["type"] == "memory_cancelled" for e in store.events)
