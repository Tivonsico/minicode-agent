"""Offline model, real project storage/tools/PowerShell integration."""

import asyncio
import json
import shutil

import pytest

from agent.loop import AgentLoop
from config import Config
from context import ContextManager
from conversation.projection import project_messages
from conversation.provider import Response
from conversation.resume import recover_session
from memory.long_memory import LongMemory
from memory.output_store import OutputStore
from memory.session_store import SessionStore
from tools.executor import ToolExecutor


def call(id, name, **arguments):
    return {
        "id": id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class ScriptedProvider:
    def __init__(self, config, calls):
        self.config = config
        self.calls = calls
        self.main_requests = []
        self.memory_requests = 0

    async def complete(self, messages, tools, on_delta, on_attempt=None, purpose="main"):
        if on_attempt:
            await on_attempt(1)
        if purpose == "memory":
            self.memory_requests += 1
            return Response('{"entries":[]}', [], "stop", {"total_tokens": 11})
        assert purpose == "main", "This integration fixture should not require compression"
        self.main_requests.append(messages)
        if len(self.main_requests) == 1:
            return Response(
                "I will read, edit, test, and remove the obsolete file.",
                self.calls,
                "tool_calls",
                {"total_tokens": 11},
            )
        await on_delta({"type": "text", "content": "Completed."})
        return Response("Completed.", [], "stop", {"total_tokens": 11})


@pytest.mark.asyncio
@pytest.mark.skip(reason="Shell execution requires a verified Windows sandbox")
async def test_real_tools_persist_complete_restore_without_replay(tmp_path):
    if not (shutil.which("pwsh") or shutil.which("powershell")):
        pytest.skip("Integration requires installed PowerShell")
    (tmp_path / "a.txt").write_text("old value", encoding="utf-8")
    (tmp_path / "b.txt").write_text("obsolete", encoding="utf-8")
    config = Config(
        root=tmp_path, api_key="offline-fixture", context_window=128000, max_output_tokens=4096
    )
    store = SessionStore(tmp_path)
    outputs = OutputStore(store)
    approvals, trace = [], []

    async def approve(request):
        approvals.append(request)
        return True

    def emit(kind, **data):
        trace.append((kind, data))
        store.append(kind, **data)

    calls = [
        call("read-a", "ReadFile", path="a.txt"),
        call("read-b", "ReadFile", path="b.txt"),
        call("edit-a", "EditFile", path="a.txt", old_text="old value", new_text="new value"),
        call(
            "shell",
            "Shell",
            command="if ((Get-Content 'a.txt' -Raw) -ne 'new value') { exit 9 }; Add-Content -Path 'runs.txt' -Value 'once'; Write-Output ('Z' * 20000)",
            timeout=120,
        ),
        call("delete-b", "DeleteFile", path="b.txt"),
    ]
    provider = ScriptedProvider(config, calls)
    memory = LongMemory(tmp_path, provider)
    executor = ToolExecutor(tmp_path, outputs, approve, emit)
    loop = AgentLoop(
        config, store, provider, executor, ContextManager(config, provider, store), memory
    )
    try:
        assert await loop.run("Edit a.txt, verify it, then remove obsolete b.txt.") == "completed"
        await asyncio.sleep(0)  # Allow the scheduled memory task to commit.
        assert provider.memory_requests == 1
        assert any(e["type"] == "memory_complete" for e in store.events)
        assert (tmp_path / "a.txt").read_text() == "new value"
        assert not (tmp_path / "b.txt").exists()
        assert (tmp_path / "runs.txt").read_text().splitlines() == ["once"]
        assert [request["name"] for request in approvals] == ["Shell", "DeleteFile"]
        assert str(tmp_path) in approvals[0]["description"]

        first_result = next(i for i, (kind, _) in enumerate(trace) if kind == "tool_result")
        assert {
            data["call_id"] for kind, data in trace[:first_result] if kind == "tool_pending"
        } == {"read-a", "read-b"}
        results = [e for e in store.events if e["type"] == "tool_result"]
        assert len(results) == len(calls)
        assert all(e["status"] == "succeeded" for e in results)
        assert [e["call_id"] for e in results[2:]] == ["edit-a", "shell", "delete-b"]
        assert store.meta["token_total"] == 33
        assert not store.meta["usage_incomplete"]
        assert store.meta["usage_by_purpose"]["memory"]["tokens"] == 11

        refs = [e for e in store.events if e["type"] == "output_saved"]
        assert refs
        assert any((store.directory / e["ref"]).stat().st_size > 16000 for e in refs)
        assert all((store.directory / e["ref"]).resolve() in outputs.allowed_paths() for e in refs)
        shell_result = next(e for e in results if e["call_id"] == "shell")
        assert len(shell_result["content"]) <= 16000
        assert "ReadFile:" in shell_result["content"]
        projected = project_messages(store.events)
        assert sorted(m["tool_call_id"] for m in projected if m["role"] == "tool") == sorted(
            c["id"] for c in calls
        )
        assert projected[-1] == {"role": "assistant", "content": "Completed."}

        restored = recover_session(SessionStore(tmp_path, store.session_id))
        assert len(restored.events) == len(store.events)
        restored_executor = ToolExecutor(
            tmp_path,
            OutputStore(restored),
            approve,
            lambda kind, **data: restored.append(kind, **data),
        )
        resumed = AgentLoop(
            config,
            restored,
            provider,
            restored_executor,
            ContextManager(config, provider, restored),
            memory,
        )
        assert await resumed.run("Continue") == "completed"
        await asyncio.sleep(0)
        assert len(approvals) == 2
        assert (tmp_path / "runs.txt").read_text().splitlines() == ["once"]
        assert len([e for e in restored.events if e["type"] == "tool_result"]) == len(calls)
    finally:
        await memory.close()
