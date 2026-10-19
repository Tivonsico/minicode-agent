"""Evidence lifecycle, manual management and offline evaluation contracts."""

import asyncio
import json

import httpx
import pytest

from backend.conversation.resume import recover_session
from backend.conversation.store import SessionStore
from backend.observability.inputs import redact
from backend.observability.requests import recorded_request
from backend.observability.scope import scope
from backend.runtime.provider import Provider, Response
from test.integration.test_web import ScriptedProvider, client, until
from test.support.tooling import call


async def completed(connection, service, text="你好"):
    sid = (await connection.post("/api/sessions")).json()["session"]["session_id"]
    await connection.post(f"/api/sessions/{sid}/runs", json={"text": text})
    await until(lambda: not service.get(sid).busy())
    return sid


async def test_accepted_input_is_durable_before_task_is_scheduled(tmp_path):
    async with client(tmp_path) as (_, service, _, _):
        item = service.create()
        item.start("accepted input before scheduling")
        crashed = tmp_path / "crashed"
        crashed.mkdir()
        copy = SessionStore(crashed, item.store.session_id)
        copy.path.write_bytes(item.store.path.read_bytes())
        reopened = SessionStore(crashed, item.store.session_id)
        recover_session(reopened)
        from backend.conversation.projection import project_messages

        assert [m["content"] for m in project_messages(reopened.events) if m["role"] == "user"] == [
            "accepted input before scheduling"
        ]
        await item.cancel()


async def test_unknown_reconciliation_is_durable_and_does_not_run_a_tool(tmp_path):
    async with client(tmp_path) as (_, service, _, _):
        item = service.create()
        sid = item.store.session_id
        item.store.append(
            "message",
            turn_id="old",
            message_id="tool-message",
            role="assistant",
            content="",
            tool_calls=[call("Shell", {"command": "echo once"}, "c")],
        )
        result = item.store.append(
            "tool_result",
            turn_id="old",
            call_id="c",
            name="Shell",
            status="unknown",
            content="missing result",
        )
    async with client(tmp_path) as (connection, service, provider, _):
        base = f"/api/sessions/{sid}"
        assert (await connection.post(base + "/runs", json={"text": "continue"})).status_code == 409
        response = await connection.post(
            base + "/operations/reconcile",
            json={
                "result_seq": result["seq"],
                "resolution": "succeeded",
                "processes_stopped": True,
                "evidence": "人工核查目标输出，命令已完成，不再执行。",
            },
        )
        assert response.status_code == 200
        assert response.json()["unresolved_operations"] == []
        assert provider.calls == 0
        assert (
            await connection.post(
                base + "/operations/reconcile",
                json={
                    "result_seq": result["seq"],
                    "resolution": "succeeded",
                    "processes_stopped": True,
                    "evidence": "重复确认",
                },
            )
        ).status_code == 409
    async with client(tmp_path) as (connection, service, _, _):
        detail = (await connection.get(f"/api/sessions/{sid}")).json()
        assert detail["unresolved_operations"] == []
        assert "人工核查" in str(detail["messages"])
        assert (
            await connection.post(f"/api/sessions/{sid}/runs", json={"text": "continue"})
        ).status_code == 202
        await until(lambda: not service.get(sid).busy())
        assert not any(e["type"] == "tool_start" for e in service.get(sid).store.events)


async def test_associations_inputs_and_read_only_export(tmp_path):
    async with client(tmp_path) as (connection, service, provider, _):
        sid = await completed(connection, service, "你好 test-api-secret")
        item = service.get(sid)
        await service.long_memory.close()
        base = f"/api/sessions/{sid}"
        events = item.store.events
        starts = [e for e in events if e["type"] == "request_start"]
        assert {e["purpose"] for e in starts} == {"main", "memory"}
        for event in starts:
            assert all(event.get(key) for key in ("run_id", "turn_id", "request_id"))
            assert event.get("step_id") or event.get("source_step_id")
            ends = [
                e
                for e in events
                if e["type"] == "request_end" and e["request_id"] == event["request_id"]
            ]
            assert len(ends) == 1 and ends[0]["status"] == "completed"
        before, calls = len(events), provider.calls
        export = (await connection.get(base + "/trajectory")).json()
        assert len(events) == before and provider.calls == calls
        assert "test-api-secret" not in json.dumps(export)
        main = next(e for e in starts if e["purpose"] == "main")
        artifact = (
            await connection.get(base + "/inputs", params={"ref": main["input_ref"]})
        ).json()
        assert artifact["payload"]["messages"][-1]["content"] == "你好 [REDACTED]"
        assert artifact["versions"] == main["input_versions"]
        assert (
            await connection.get(base + "/inputs", params={"ref": "../task_state.json"})
        ).status_code == 409
        assert len(export["turns"]) == 1 and export["metrics"]["requests"] == 2
        assert all(e["duration_ms"] >= 0 for e in events if e["type"] == "step_end")


async def test_sync_running_state_cancel_and_stale_edit(tmp_path):
    provider = ScriptedProvider()
    provider.block = asyncio.Event()
    async with client(tmp_path, provider) as (connection, service, _, _):
        item = service.create()
        base = f"/api/sessions/{item.store.session_id}"
        item.start("等待模型")
        await until(lambda: provider.calls == 1)
        snapshot = (await connection.post(base + "/checkpoints/sync")).json()
        assert snapshot["source"] == "manual" and snapshot["state"]["status"] == "running"
        assert snapshot["covered_seq"] == snapshot["seq"] - 1
        data = (await connection.get(base + "/diagnostics")).json()
        edit = {
            "expected_seq": data["state_revision"],
            "objective": "人工目标",
            "notes": "检查",
            "next_step": "继续",
            "status": "completed",
        }
        assert (await connection.put(base + "/checkpoint-state", json=edit)).status_code == 409
        edit["status"] = "running"
        assert (await connection.put(base + "/checkpoint-state", json=edit)).status_code == 200
        assert (await connection.put(base + "/checkpoint-state", json=edit)).status_code == 409
        assert (
            await connection.post(
                "/api/evaluations/runs", json={"session_ids": [item.store.session_id]}
            )
        ).status_code == 409
        await item.cancel()
        for start, end, key in (
            ("request_start", "request_end", "request_id"),
            ("step_start", "step_end", "step_id"),
            ("run_start", "run_end", "run_id"),
        ):
            started = [e for e in item.store.events if e["type"] == start]
            assert started
            for record in started:
                assert (
                    len(
                        [e for e in item.store.events if e["type"] == end and e[key] == record[key]]
                    )
                    == 1
                )
        assert (
            next(e for e in item.store.events if e["type"] == "request_end")["status"]
            == "cancelled"
        )


async def test_approval_decision_keeps_full_origin(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    provider = ScriptedProvider()
    provider.responses = [
        Response(
            "",
            [call("WriteFile", {"path": str(project / "note"), "content": "abc"}, "call-1")],
            "tool_calls",
            {},
        )
    ]
    async with client(tmp_path, provider) as (connection, service, _, _):
        item = service.create()
        item.permissions.add(str(project))
        item.start("写文件")
        await until(lambda: bool(item.approvals.listing()))
        ticket = item.approvals.listing()[0]
        item.approvals.resolve(ticket["id"], item.run_id, False)
        await until(lambda: not item.busy())
        requested = next(e for e in item.store.events if e["type"] == "approval_requested")
        decision = next(e for e in item.store.events if e["type"] == "approval_decision")
        for key in ("run_id", "turn_id", "step_id", "request_id", "call_id", "approval_id"):
            assert requested[key] and requested[key] == decision[key]
        closed = next(e for e in item.store.events if e["type"] == "approval_closed")
        assert closed["outcome"] == "rejected" and closed["duration_ms"] >= 0
        assert not (project / "note").exists()


async def test_memory_edit_versions_and_empty_rejection(tmp_path):
    async with client(tmp_path) as (connection, service, provider, _):
        sid = service.create().store.session_id
        assert (
            await connection.post("/api/memory", json={"session_id": sid, "content": "  "})
        ).status_code == 422
        entry = (
            await connection.post("/api/memory", json={"session_id": sid, "content": "优先中文"})
        ).json()
        url = f"/api/memory/{entry['id']}"
        body = {"content": "中文并保留代码术语", "version": 1}
        response = await connection.put(url, json=body)
        assert response.status_code == 200 and response.json()["version"] == 2
        assert (await connection.put(url, json=body)).status_code == 409
        assert (await connection.delete(url)).status_code == 200
        assert (await connection.put(url, json={**body, "version": 2})).status_code == 404
        assert provider.calls == 0


async def test_rule_reports_frozen_and_completion_not_verification(tmp_path):
    async with client(tmp_path) as (connection, service, provider, _):
        sid = await completed(connection, service)
        await service.long_memory.close()
        dataset = (await connection.get("/api/evaluations/dataset")).json()
        dataset["cases"] = [
            {
                "id": "greeting",
                "name": "任务完成验证",
                "input_contains": ["你好"],
                "output_contains": ["任务完成"],
                "require_verification": True,
            }
        ]
        saved = (await connection.put("/api/evaluations/dataset", json=dataset)).json()
        assert saved["revision"] == dataset["revision"] + 1
        assert (await connection.put("/api/evaluations/dataset", json=dataset)).status_code == 409
        item = service.get(sid)
        calls, seq = provider.calls, len(item.store.events)
        report = (
            await connection.post("/api/evaluations/runs", json={"session_ids": [sid, sid]})
        ).json()
        assert report["metrics"]["evaluated"] == 1 and report["metrics"]["rule_pass_rate"] == 0
        assert report["sources"] == [{"session_id": sid, "covered_seq": seq}]
        assert provider.calls == calls and len(item.store.events) == seq
        turn_id = report["results"][0]["turn_id"]
        item.store.append(
            "verification_result",
            turn_id=turn_id,
            source="execution",
            status="passed",
            call_id="actual-test",
        )
        second = (
            await connection.post("/api/evaluations/runs", json={"session_ids": [sid]})
        ).json()
        assert second["metrics"]["rule_pass_rate"] == 1
        frozen = (await connection.get(f"/api/evaluations/reports/{report['id']}")).json()
        assert frozen == report


async def test_history_evaluation_only_grades_selected_rules(tmp_path):
    async with client(tmp_path) as (connection, service, provider, _):
        sid = await completed(connection, service)
        await service.long_memory.close()
        dataset = (await connection.get("/api/evaluations/dataset")).json()
        dataset["cases"] = [
            {"id": "pass", "name": "完成", "expected_status": "completed"},
            {"id": "fail", "name": "未满足的回复", "output_contains": ["不存在的答案"]},
        ]
        saved = (await connection.put("/api/evaluations/dataset", json=dataset)).json()
        before, calls = len(service.get(sid).store.events), provider.calls
        report = (
            await connection.post(
                "/api/evaluations/runs",
                json={"session_ids": [sid], "case_ids": ["pass", "pass"]},
            )
        ).json()
        assert report["metrics"]["rule_pass_rate"] == 1
        assert [row["case_id"] for row in report["results"]] == ["pass"]
        assert [case["id"] for case in report["dataset"]["cases"]] == ["pass"]
        assert report["dataset_revision"] == saved["revision"]
        assert (await connection.get("/api/evaluations/dataset")).json() == saved
        assert len(service.get(sid).store.events) == before and provider.calls == calls
        # Older clients may omit the selection; that still checks all history rules.
        full = (await connection.post("/api/evaluations/runs", json={"session_ids": [sid]})).json()
        assert full["metrics"]["evaluated"] == 2 and full["metrics"]["rule_pass_rate"] == 0.5
        assert (await connection.get(f"/api/evaluations/reports/{report['id']}")).json() == report


@pytest.mark.parametrize("selection", [[], ["unknown"], ["coding"]])
async def test_history_selection_rejects_empty_missing_and_coding_cases(tmp_path, selection):
    async with client(tmp_path) as (connection, service, provider, _):
        sid = service.create().store.session_id
        dataset = (await connection.get("/api/evaluations/dataset")).json()
        if selection == ["coding"]:
            selection = [next(c["id"] for c in dataset["cases"] if c["mode"] == "task")]
        response = await connection.post(
            "/api/evaluations/runs", json={"session_ids": [sid], "case_ids": selection}
        )
        assert response.status_code == 422
        assert (await connection.get("/api/evaluations/reports")).json() == []
        assert provider.calls == 0


async def test_crash_closes_each_lifecycle_once(tmp_path):
    store = SessionStore(tmp_path, "crashed")
    tags = dict(run_id="r", turn_id="t", step_id="s", request_id="q", call_id="c", approval_id="a")
    for kind in (
        "turn_start",
        "run_accepted",
        "run_start",
        "step_start",
        "request_start",
        "tool_start",
        "tool_running",
        "approval_requested",
    ):
        store.append(kind, **tags, name="Shell")
    recover_session(store)
    count = len(store.events)
    recover_session(store)
    assert len(store.events) == count
    assert next(e for e in store.events if e["type"] == "tool_result")["status"] == "unknown"
    assert (
        next(e for e in store.events if e["type"] == "approval_closed")["status"] == "interrupted"
    )
    from backend.observability.trajectory import project

    assert project(store.events)["metrics"]["approval_wait_ms"] == 0


async def test_real_transport_retry_attempts_have_unique_ends_and_saved_payload(tmp_path):
    from backend.config import Config

    config = Config(tmp_path, "secret", context_window=100000, max_output_tokens=1000)
    store = SessionStore(tmp_path, "transport")
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(503)
        data = {
            "choices": [{"delta": {"content": "你好"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 4},
        }
        return httpx.Response(200, text="data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n")

    provider = Provider(config, transport=httpx.MockTransport(handler), retry_delay=0)
    with scope(store, run_id="r", turn_id="t", step_id="s"):
        result = await recorded_request(
            provider,
            store,
            [{"role": "user", "content": "你好"}],
            [],
            on_delta=lambda event: asyncio.sleep(0),
            config=config,
        )
    starts = [e for e in store.events if e["type"] == "request_start"]
    ends = [e for e in store.events if e["type"] == "request_end"]
    assert len(starts) == len(ends) == 2
    assert starts[0]["request_id"] != starts[1]["request_id"]
    assert ends[0]["status"] == "failed" and ends[0]["retrying"]
    assert ends[1]["status"] == "completed" and result.request_id == starts[1]["request_id"]
    assert starts[0]["input_ref"] == starts[1]["input_ref"]


def test_structured_redaction_and_private_key():
    value = {
        "password": "hidden",
        "content": "api_key=secret",
        "key": "-----BEGIN PRIVATE KEY-----abc-----END PRIVATE KEY-----",
    }
    result = redact(value)
    assert (
        result["password"] == "[REDACTED]"
        and "secret" not in str(result)
        and "abc" not in str(result)
    )


async def test_disconnect_after_partial_output_is_ended_without_retry(tmp_path):
    import pytest

    from backend.config import Config
    from backend.runtime.provider import ProviderError

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
            raise httpx.ReadError("connection lost")

    config = Config(tmp_path, "secret", context_window=100000, max_output_tokens=1000)
    store = SessionStore(tmp_path, "broken")
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=BrokenStream())

    provider = Provider(config, transport=httpx.MockTransport(handler), retry_delay=0)
    with scope(store, run_id="r", turn_id="t", step_id="s"):
        with pytest.raises(ProviderError):
            await recorded_request(
                provider, store, [], [], lambda e: asyncio.sleep(0), config=config
            )
    ends = [e for e in store.events if e["type"] == "request_end"]
    assert calls == len(ends) == 1
    assert ends[0]["status"] == "failed" and not ends[0]["retrying"]
    assert any(e["type"] == "request_progress" and e["kind"] == "text" for e in store.events)


async def test_background_memory_keeps_original_turn_during_next_run(tmp_path):
    class DelayedMemory(ScriptedProvider):
        def __init__(self):
            super().__init__()
            self.memory_started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, messages, tools, on_delta=None, on_attempt=None, purpose="main"):
            if purpose != "memory":
                return await super().complete(messages, tools, on_delta, on_attempt, purpose)
            await on_attempt(1)
            self.memory_started.set()
            await self.release.wait()
            return Response('{"entries":[]}', [], "stop", {"total_tokens": 1})

    provider = DelayedMemory()
    async with client(tmp_path, provider) as (connection, service, _, _):
        sid = await completed(connection, service)
        item = service.get(sid)
        await provider.memory_started.wait()
        first_run = item.run_id
        first_turn = item.turn_id
        item.start("第二轮")
        await until(lambda: not item.busy())
        assert item.run_id != first_run and item.turn_id != first_turn
        provider.release.set()
        await service.long_memory.close()
        starts = [
            e
            for e in item.store.events
            if e["type"] == "request_start" and e["purpose"] == "memory"
        ]
        first = next(e for e in starts if e["turn_id"] == first_turn)
        end = next(
            e
            for e in item.store.events
            if e["type"] == "request_end" and e["request_id"] == first["request_id"]
        )
        assert end["turn_id"] == first_turn and end["run_id"] == first_run
