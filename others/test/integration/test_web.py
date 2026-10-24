"""Offline API tests: runtime ownership, approvals, recovery, isolation and assets."""

import asyncio
from contextlib import asynccontextmanager

import httpx

from backend.api.app import create_app
from backend.config import Config
from backend.runtime.provider import Response
from test.support.tooling import call


class ScriptedProvider:
    def __init__(self):
        self.calls = 0
        self.responses = []
        self.block = None
        self.skill_contexts = []

    async def complete(self, messages, tools, on_delta=None, on_attempt=None, purpose="main"):
        if on_attempt:
            await on_attempt(1)
        if purpose == "memory":
            return Response('{"entries":[]}', [], "stop", {"total_tokens": 1})
        self.calls += 1
        self.skill_contexts.append(messages[0]["content"])
        if self.block:
            await self.block.wait()
        response = (
            self.responses.pop(0)
            if self.responses
            else Response("任务完成", [], "stop", {"total_tokens": 3})
        )
        if on_delta and response.content:
            await on_delta({"type": "text", "content": response.content})
        return response


@asynccontextmanager
async def client(tmp_path, provider=None):
    root = tmp_path / "app"
    root.mkdir(exist_ok=True)
    provider = provider or ScriptedProvider()
    app = create_app(
        Config(root, "test-api-secret", context_window=100_000, max_output_tokens=1000),
        provider,
        initialize_sandbox=False,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"X-Minicode-Token": app.state.token},
        ) as connection:
            yield connection, app.state.service, provider, app


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_bootstrap_security_and_no_credentials(tmp_path):
    async with client(tmp_path) as (connection, service, provider, app):
        page = await connection.get("/")
        assert page.status_code == 200 and app.state.token in page.text
        assert service.config.api_key not in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert (
            await connection.get("/api/info", headers={"X-Minicode-Token": "wrong"})
        ).status_code == 403
        assert (
            await connection.get("/api/info", headers={"Origin": "https://evil.test"})
        ).status_code == 403
        assert (await connection.get("/", headers={"Host": "evil.test"})).status_code == 403
        assert (
            await connection.post("/api/sessions", headers={"Sec-Fetch-Site": "cross-site"})
        ).status_code == 403
        assert provider.calls == 0


async def test_storage_settings_can_be_saved_with_an_open_local_session(tmp_path):
    async with client(tmp_path) as (connection, service, _, _):
        created = await connection.post("/api/sessions")
        assert created.status_code == 201
        response = await connection.put("/api/storage", json={"storage_mode": "local"})
        assert response.status_code == 200
        assert response.json()["active_mode"] == "local"


async def test_run_survives_http_client_switch_and_cancel_preserves_partial(tmp_path):
    provider = ScriptedProvider()
    provider.block = asyncio.Event()
    async with client(tmp_path, provider) as (connection, service, _, app):
        data = (await connection.post("/api/sessions")).json()
        sid = data["session"]["session_id"]
        response = await connection.post(f"/api/sessions/{sid}/runs", json={"text": "一个慢任务"})
        assert response.status_code == 202
        await until(lambda: provider.calls == 1)
        assert (
            await connection.post(f"/api/sessions/{sid}/runs", json={"text": "冲突任务"})
        ).status_code == 409
        # A second client observes the same backend task; reconnecting never invokes a model.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"X-Minicode-Token": app.state.token},
        ) as other:
            snapshot = (await other.get(f"/api/sessions/{sid}")).json()
            assert snapshot["running"] and snapshot["messages"][-1]["content"] == "一个慢任务"
            assert provider.calls == 1
            ended = (await other.post(f"/api/sessions/{sid}/cancel")).json()
            assert not ended["running"] and service.get(sid).loop.status == "interrupted"
        assert (await connection.get("/api/sessions")).json()[0]["title"] == "一个慢任务"


async def test_directory_approval_diff_single_use_and_completed_task(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.txt"
    target.write_text("before")
    provider = ScriptedProvider()
    provider.responses = [
        Response("", [call("ReadFile", {"path": str(target)}, "read")], "tool_calls", {}),
        Response(
            "",
            [
                call(
                    "EditFile",
                    {"path": str(target), "old_text": "before", "new_text": "after"},
                    "edit",
                )
            ],
            "tool_calls",
            {},
        ),
    ]
    async with client(tmp_path, provider) as (connection, service, _, app):
        sid = (await connection.post("/api/sessions")).json()["session"]["session_id"]
        base = f"/api/sessions/{sid}"
        assert (
            await connection.post(
                base + "/directories", json={"action": "add", "path": str(project)}
            )
        ).status_code == 200
        result = (await connection.post(base + "/runs", json={"text": "修改 note"})).json()
        item = service.get(sid)
        await until(lambda: bool(item.approvals.listing()))
        ticket = item.approvals.listing()[0]
        assert "-before" in ticket["details"] and "+after" in ticket["details"]
        assert target.read_text() == "before"
        assert (
            await connection.post(
                base + "/directories", json={"action": "remove", "path": str(project)}
            )
        ).status_code == 409
        assert (
            await connection.post(
                base + f"/approvals/{ticket['id']}", json={"run_id": "old-run", "approved": True}
            )
        ).status_code == 409
        assert target.read_text() == "before"
        response = await connection.post(
            base + f"/approvals/{ticket['id']}", json={"run_id": result["run_id"], "approved": True}
        )
        assert response.status_code == 200
        await until(lambda: not item.busy())
        assert target.read_text() == "after"
        assert item.loop.status == "completed"
        assert (
            await connection.post(
                base + f"/approvals/{ticket['id']}",
                json={"run_id": result["run_id"], "approved": True},
            )
        ).status_code == 409
        second = (await connection.post("/api/sessions")).json()
        assert second["directories"] == []
        assert (await connection.get(base)).json()["directories"][0]["active"]


async def test_reject_and_cancel_pending_approval_cannot_mutate(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    provider = ScriptedProvider()
    async with client(tmp_path, provider) as (connection, service, _, app):
        sid = (await connection.post("/api/sessions")).json()["session"]["session_id"]
        base = f"/api/sessions/{sid}"
        await connection.post(base + "/directories", json={"action": "add", "path": str(project)})
        for accepted in (False, None):
            provider.responses = [
                Response(
                    "",
                    [
                        call(
                            "WriteFile",
                            {"path": str(project / "new.txt"), "content": "unsafe"},
                            f"write-{accepted}",
                        )
                    ],
                    "tool_calls",
                    {},
                )
            ]
            run = (await connection.post(base + "/runs", json={"text": "写文件"})).json()
            item = service.get(sid)
            await until(lambda: bool(item.approvals.listing()))
            ticket = item.approvals.listing()[0]
            if accepted is None:
                await connection.post(base + "/cancel")
            else:
                await connection.post(
                    base + f"/approvals/{ticket['id']}",
                    json={"run_id": run["run_id"], "approved": False},
                )
            await until(lambda: not item.busy())
            assert not (project / "new.txt").exists()
            assert not item.approvals.listing()
            assert item.loop.status == (
                "interrupted" if accepted is None else "paused"
            )


async def test_skill_one_turn_prompt_edit_and_directory_boundaries(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    async with client(tmp_path) as (connection, service, provider, app):
        skill = service.config.root / "skill" / "skills" / "example"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: example\ndescription: test skill\n---\nFORCED_SKILL_BODY"
        )
        sid = (await connection.post("/api/sessions")).json()["session"]["session_id"]
        base = f"/api/sessions/{sid}"
        assert (
            await connection.post(
                base + "/directories", json={"action": "add", "path": str(service.config.root)}
            )
        ).status_code == 400
        await connection.post(base + "/directories", json={"action": "add", "path": str(project)})
        assert (
            await connection.post(base + "/skills", json={"name": "example"})
        ).status_code == 200
        assert (
            await connection.put(
                base + "/prompts", json={"scope": "global", "content": "GLOBAL_RULE"}
            )
        ).status_code == 200
        assert (
            await connection.put(
                base + "/prompts",
                json={"scope": "project", "project_path": str(project), "content": "PROJECT_RULE"},
            )
        ).status_code == 200
        for index in range(2):
            await connection.post(base + "/runs", json={"text": "任务"})
            await until(lambda: not service.get(sid).busy())
            assert (
                "GLOBAL_RULE" in provider.skill_contexts[index]
                and "PROJECT_RULE" in provider.skill_contexts[index]
            )
        assert (
            "FORCED_SKILL_BODY" in provider.skill_contexts[0]
            and "FORCED_SKILL_BODY" not in provider.skill_contexts[1]
        )
        await connection.post(
            base + "/directories", json={"action": "remove", "path": str(project)}
        )
        assert (await connection.get(base)).json()["directories"] == []


async def test_event_replay_and_crash_recovery_never_reexecutes_tool(tmp_path):
    from backend.api.events import event_response
    from backend.conversation.store import SessionStore

    async with client(tmp_path) as (connection, service, provider, app):
        store = SessionStore(service.config.root, "recovered")
        store.append("turn_start", turn_id="t")
        store.append(
            "message",
            message_id="u",
            turn_id="t",
            role="user",
            content="恢复任务",
            status="complete",
        )
        store.append(
            "assistant_delta", turn_id="t", message_id="a", kind="text", content="部分输出"
        )
        recovered = service.get("recovered")
        assert next(e for e in reversed(recovered.store.view) if e["type"] == "turn_end")["status"] == "interrupted"
        assert recovered.loop.conversation.messages()[-1]["content"].startswith("部分输出")
        assert provider.calls == 0
        response = event_response(recovered, 2)
        iterator = response.body_iterator
        first = await anext(iterator)
        assert "id: 3\n" in first and "部分输出" in first
        await iterator.aclose()
        assert not recovered.busy()


async def test_cancel_before_scheduled_coroutine_starts_is_settled(tmp_path):
    async with client(tmp_path) as (connection, service, provider, app):
        item = service.create()
        item.start("尚未派发的任务")
        await item.cancel()
        assert not item.busy()
        assert item.loop.status == "interrupted"
        assert provider.calls == 0


async def test_browser_and_skill_creation_edit_selection_are_session_scoped(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "src").mkdir()
    async with client(tmp_path) as (connection, service, provider, app):
        sid = (await connection.post("/api/sessions")).json()["session"]["session_id"]
        base = f"/api/sessions/{sid}"
        browsing = await connection.get(base + "/directory-browser", params={"path": str(project)})
        assert browsing.status_code == 200 and browsing.json()["entries"][0]["name"] == "src"
        assert service.get(sid).permissions.roots() == ()
        body = {
            "name": "review",
            "description": "Review code",
            "content": "Check edge cases.",
            "scope": "project",
            "project_path": str(project),
        }
        assert (await connection.post(base + "/skill-library", json=body)).status_code == 400
        await connection.post(base + "/directories", json={"action": "add", "path": str(project)})
        assert (await connection.post(base + "/skill-library", json=body)).status_code == 200
        assert (await connection.post(base + "/skill-library", json=body)).status_code == 400
        content = (
            await connection.get(
                base + "/skill-library/review",
                params={"scope": "project", "project_path": str(project)},
            )
        ).json()["content"]
        changed = content.replace("Check edge cases.", "Check cancellation.")
        assert (
            await connection.put(
                base + "/skill-library/review",
                json={"scope": "project", "project_path": str(project), "content": changed},
            )
        ).status_code == 200
        assert (
            await connection.post(
                base + "/skills",
                json={"name": "review", "scope": "project", "project_path": str(project)},
            )
        ).status_code == 200
        item = service.get(sid)
        item.start("Use the review skill")
        await until(lambda: not item.busy())
        assert "Check cancellation." in provider.skill_contexts[0]
        other = (await connection.post("/api/sessions")).json()
        assert (
            await connection.get(f"/api/sessions/{other['session']['session_id']}/skills")
        ).json() == []


async def test_skill_global_mutation_rejected_while_another_session_runs(tmp_path):
    provider = ScriptedProvider()
    provider.block = asyncio.Event()
    async with client(tmp_path, provider) as (connection, service, _, app):
        first, second = service.create(), service.create()
        first.start("slow")
        body = {"name": "review", "description": "Review", "content": "Rules"}
        response = await connection.post(
            f"/api/sessions/{second.store.session_id}/skill-library", json=body
        )
        assert response.status_code == 409
        assert not (service.config.root / "skill/skills/review").exists()
        await first.cancel()


async def test_skill_download_revalidates_grant_after_network_wait(tmp_path, monkeypatch):
    import backend.api.assets as assets

    started, release = asyncio.Event(), asyncio.Event()

    async def fake_bundle(*args):
        started.set()
        await release.wait()
        return {"SKILL.md": b"description: Review\nRules"}

    monkeypatch.setattr(assets, "bundle", fake_bundle)
    project = tmp_path / "project"
    project.mkdir()
    async with client(tmp_path) as (connection, service, provider, app):
        item = service.create()
        item.permissions.add(str(project))
        base = f"/api/sessions/{item.store.session_id}"
        body = {
            "repository": "owner/repo",
            "revision": "a" * 40,
            "path": "skills/review",
            "name": "review",
            "scope": "project",
            "project_path": str(project),
        }
        download = asyncio.create_task(connection.post(base + "/skill-download", json=body))
        await started.wait()
        item.permissions.remove(str(project))
        release.set()
        assert (await download).status_code == 400
        assert not (project / "project_skills/review").exists()


async def test_storage_configuration_is_validated_and_requires_restart(tmp_path):
    async with client(tmp_path) as (connection, service, _, app):
        initial = (await connection.get("/api/storage")).json()
        assert initial["active_mode"] == "local" and not initial["connected"]
        invalid = await connection.put("/api/storage", json={"storage_mode": "mysql"})
        assert invalid.status_code == 409
        saved = await connection.put("/api/storage", json={"storage_mode": "local",
            "mysql_password": "never-show-this"})
        assert saved.status_code == 200
        assert "never-show-this" not in saved.text
        assert saved.json()["active_mode"] == "local"
        from backend.config import load_config
        env_file = service.config.root / ".env"
        env_file.write_text("OPENAI_API_KEY=test\nMODEL=test-model\n")
        loaded = load_config(service.config.root)
        assert loaded.mysql_password == "never-show-this"
        import os
        if os.name != "nt":
            assert (service.config.root / ".minicode" / "storage.json").stat().st_mode & 0o777 == 0o600
