"""Reasoning levels reach actual requests without leaking between concurrent runs."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx

from backend.api.app import create_app
from backend.config import Config
from backend.runtime.provider import Provider
from test.integration.test_web import until


@asynccontextmanager
async def application(tmp_path):
    sent = []
    config = Config(
        tmp_path, "secret", model="GLM-5.3-Flash", context_window=100000, max_output_tokens=1000
    )

    async def handler(request):
        payload = json.loads(request.content)
        sent.append(payload)
        await asyncio.sleep(0.01)
        memory = "Extract only" in payload["messages"][0]["content"]
        value = '{"entries":[]}' if memory else "完成"
        chunk = {
            "choices": [{"delta": {"content": value}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 2},
        }
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    provider = Provider(config, transport=httpx.MockTransport(handler), retry_delay=0)
    app = create_app(config, provider, initialize_sandbox=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"X-Minicode-Token": app.state.token},
        ) as connection:
            yield connection, app.state.service, sent


async def test_official_levels_validation_default_and_saved_actual_input(tmp_path):
    async with application(tmp_path) as (connection, service, sent):
        policy = (await connection.get("/api/info")).json()["reasoning"]
        assert [item["value"] for item in policy["levels"]] == ["low", "high", "max"]
        assert policy["default"] == "max"
        sid = service.create().store.session_id
        base = f"/api/sessions/{sid}"
        assert (
            await connection.post(
                base + "/runs", json={"text": "test", "reasoning_effort": "medium"}
            )
        ).status_code == 409
        assert not sent and not service.get(sid).busy()
        for value in ("low", "high", None):
            body = {"text": "test"}
            if value is not None:
                body["reasoning_effort"] = value
            assert (await connection.post(base + "/runs", json=body)).status_code == 202
            item = service.get(sid)
            await until(lambda: not item.busy())
            main = next(
                e
                for e in reversed(item.store.events)
                if e["type"] == "request_start" and e["purpose"] == "main"
            )
            artifact = (
                await connection.get(base + "/inputs", params={"ref": main["input_ref"]})
            ).json()
            assert artifact["payload"]["reasoning_effort"] == (value or "max")
            assert main["reasoning_effort"] == (value or "max")
        await service.long_memory.close()
        assert {payload["reasoning_effort"] for payload in sent} == {"low", "high", "max"}


async def test_concurrent_sessions_keep_main_and_background_reasoning_separate(tmp_path):
    async with application(tmp_path) as (connection, service, sent):
        first, second = service.create(), service.create()
        first.start("first", "low")
        second.start("second", "high")
        await until(lambda: not first.busy() and not second.busy())
        await service.long_memory.close()
        for item, level in ((first, "low"), (second, "high")):
            starts = [e for e in item.store.events if e["type"] == "request_start"]
            assert {e["purpose"] for e in starts} == {"main", "memory"}
            assert all(e["reasoning_effort"] == level for e in starts)
            for record in starts:
                data = json.loads((item.store.directory / record["input_ref"]).read_text())
                assert data["payload"]["reasoning_effort"] == level
        assert len(sent) == 4
