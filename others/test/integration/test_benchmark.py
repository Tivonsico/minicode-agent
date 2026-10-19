"""The benchmark measures Agent outputs; these tests validate the measurement."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.evaluation.catalog import builtin_cases
from backend.evaluation.failure import classify, request_error
from backend.evaluation.metrics import summarize
from backend.evaluation.models import Case
from backend.evaluation.repository import EvaluationRepository
from backend.evaluation.task_input import task_input
from backend.evaluation.verifier import same, verify
from backend.runtime.provider import Response
from test.integration.test_shell import verified
from test.integration.test_web import ScriptedProvider, client, until
from test.support.tooling import call


async def test_all_30_gold_solutions_pass_and_starters_fail(tmp_path):
    sandbox, work, _ = await verified(tmp_path)
    gold = json.loads((Path(__file__).parents[1] / "fixtures/benchmark_solutions.json").read_text())
    cases = [c for c in builtin_cases() if c["goal"] == "implementation"]
    assert len(cases) == 30 and len({c["id"] for c in cases}) == 30
    for case in cases:
        for name, text in case["files"].items():
            (work / name).write_text(text)
        before = await verify(case, work, sandbox)
        assert not all(check["passed"] for check in before), case["id"]
        for name, text in gold[case["id"]].items():
            (work / name).write_text(text)
        after = await verify(case, work, sandbox)
        assert all(check["passed"] for check in after), (case["id"], after)


async def test_verifier_cannot_write_workspace_or_read_application(tmp_path):
    sandbox, work, _ = await verified(tmp_path)
    (sandbox.application_root / "secret.txt").write_text("SECRET")
    candidate = "from pathlib import Path\ndef probe():\n    Path('tampered').write_text('bad')\n    return True\n"
    (work / "solution.py").write_text(candidate)
    case = Case(
        id="probe",
        name="probe",
        mode="task",
        prompts=["probe"],
        assertions=[{"name": "probe", "symbol": "probe", "expected": True}],
    ).model_dump()
    result = await verify(case, work, sandbox)
    assert not result[0]["passed"] and not (work / "tampered").exists()
    candidate = (
        "from pathlib import Path\ndef probe():\n"
        f"    return Path({str(sandbox.application_root / 'secret.txt')!r}).read_text()\n"
    )
    (work / "solution.py").write_text(candidate)
    case["assertions"][0]["expected"] = "SECRET"
    result = await verify(case, work, sandbox)
    assert not result[0]["passed"]


def test_numeric_values_match_without_boolean_or_shape_coercion():
    assert same(0.0, 0) and same({"v": [1.0, 0]}, {"v": [1, 0.0]})
    assert not same(True, 1) and not same([1], 1) and not same("0", 0)


def test_transport_failure_is_not_an_unexposed_attack():
    events = [
        {"type": "request_end", "status": "failed", "error_type": "ReadTimeout"},
        {"type": "request_end", "status": "failed", "error_type": "ConnectError"},
    ]
    assert request_error(events) == "ConnectError"
    assert classify("security", False, False, False, request_error(events)) == "request_failed"
    assert classify("security", False, True, False) == "not_exposed"
    assert classify("security", True, False, True, "ConnectError") == "security_violation"
    events.append({"type": "request_end", "status": "succeeded"})
    assert request_error(events) is None
    assert classify("feature", False, True, False) == "acceptance_failed"


def test_model_task_input_has_files_but_no_private_checks_or_answers():
    case = {
        "files": {"existing.py": "PRIVATE_INITIAL_CONTENT"},
        "prompts": ["实现公开功能", "保留先前约束"],
        "assertions": [{"args": ["PRIVATE_TEST_INPUT"], "expected": "PRIVATE_EXPECTED"}],
    }
    text = task_input(case, 1)
    assert "existing.py" in text and "保留先前约束" in text
    assert "PRIVATE_" not in text


def test_task_upgrade_preserves_custom_edits_and_reports(tmp_path):
    repository = EvaluationRepository(tmp_path)
    data = repository.dataset()
    defaults = Path("backend/evaluation/data/task_updates.json")
    old_prompts = json.loads(defaults.read_text())
    data["builtin_version"] = 1
    for case in data["cases"]:
        if case["id"] in old_prompts:
            case["prompts"] = old_prompts[case["id"]]
        if case["id"] == "multi-sort":
            case["name"] = "用户编辑过的排序任务"
    repository.dataset_path.write_text(json.dumps(data))
    report = repository.save_report({"dataset": data})
    upgraded = EvaluationRepository(tmp_path).dataset()
    assert upgraded["builtin_version"] == 3 and upgraded["revision"] == data["revision"] + 1
    cases = {case["id"]: case for case in upgraded["cases"]}
    assert "solution.py" in cases["feature-csv"]["prompts"][0]
    assert cases["multi-sort"]["prompts"] == old_prompts["multi-sort"]
    assert repository.report(report["id"]) == report
    assert len(upgraded["cases"]) == 34


def test_dataset_rejects_escape_and_coding_without_acceptance():
    with pytest.raises(ValidationError):
        Case(
            id="bad",
            name="bad",
            mode="task",
            prompts=["task"],
            files={"../outside": "bad"},
            assertions=[{"name": "x", "symbol": "x"}],
        )
    with pytest.raises(ValidationError):
        Case(id="bad", name="bad", mode="task", prompts=["task"])


async def test_import_history_then_run_isolated_agent_and_frozen_report(tmp_path):
    async with client(tmp_path) as (connection, service, provider, _):
        assert await service.sandbox.verify(), service.sandbox.reason
        dataset = (await connection.get("/api/evaluations/dataset")).json()
        assert len(dataset["cases"]) == 34
        source = service.create()
        source.start("用户任务")
        await until(lambda: not source.busy())
        seq = len(source.store.events)
        dataset = (
            await connection.post(
                "/api/evaluations/imports",
                json={"session_id": source.store.session_id, "revision": dataset["revision"]},
            )
        ).json()
        assert len(dataset["cases"]) == 35 and dataset["cases"][-1]["source"]["covered_seq"] == seq
        assert dataset["cases"][-1]["mode"] == "history"
        selected = dataset["cases"][0]
        solution = "def average(values):\n    return sum(values)/len(values) if values else 0\n"
        provider.responses = [
            Response(
                "",
                [call("ReadFile", {"path": "solution.py"}, "read")],
                "tool_calls",
                {"total_tokens": 2},
            ),
            Response(
                "",
                [
                    call("WriteFile", {"path": "helper.py", "content": solution}, "helper"),
                    call(
                        "WriteFile",
                        {"path": "solution.py", "content": "from helper import average\n"},
                        "write",
                    ),
                ],
                "tool_calls",
                {"total_tokens": 2},
            ),
            Response(
                "",
                [
                    call(
                        "Shell",
                        {
                            "command": "python3 -c 'from solution import average; assert average([])==0; assert average([2,4])==3'",
                            "purpose": "verification",
                        },
                        "verify",
                    )
                ],
                "tool_calls",
                {"total_tokens": 2},
            ),
            Response("完成", [], "stop", {"total_tokens": 2}),
        ]
        response = await connection.post(
            "/api/evaluations/jobs", json={"case_ids": [selected["id"]], "agent_modes": ["manual"]}
        )
        assert response.status_code == 202, response.text
        job = response.json()
        await until(lambda: service.evaluations.tasks[job["id"]].done())
        finished = (await connection.get("/api/evaluations/jobs/" + job["id"])).json()
        assert finished["status"] == "completed", finished
        assert finished["metrics"]["task_success_rate"] == 1, finished["results"]
        assert len(source.store.events) == seq and len(service.sessions) == 1
        artifact = (await connection.get(f"/api/evaluations/jobs/{job['id']}/attempts/0")).json()
        assert artifact["initial"]["solution.py"] != artifact["final"]["solution.py"]
        assert any(e["type"] == "approval_decision" and e["approved"] for e in artifact["events"])
        assert not any(e.get("purpose") == "memory" for e in artifact["events"])
        report = (await connection.get("/api/evaluations/reports/" + finished["report_id"])).json()
        calls_before = provider.calls
        regraded = (await connection.post(f"/api/evaluations/jobs/{job['id']}/regrade")).json()
        assert regraded["metrics"]["task_success_rate"] == 1
        assert regraded["regraded_from"] == report["id"] and regraded["id"] != report["id"]
        assert regraded["results"][0]["duration_ms"] == report["results"][0]["duration_ms"]
        assert provider.calls == calls_before
        dataset["cases"][0]["prompts"] = ["改变需求"]
        await connection.put("/api/evaluations/dataset", json=dataset)
        assert (
            await connection.get("/api/evaluations/reports/" + finished["report_id"])
        ).json() == report


async def test_imported_history_is_bound_to_its_session_and_cutoff(tmp_path):
    async with client(tmp_path) as (connection, service, _, _):
        first, second = service.create(), service.create()
        first.start("同样任务")
        second.start("同样任务")
        await until(lambda: not first.busy() and not second.busy())
        dataset = (await connection.get("/api/evaluations/dataset")).json()
        imported = (
            await connection.post(
                "/api/evaluations/imports",
                json={"session_id": first.store.session_id, "revision": dataset["revision"]},
            )
        ).json()["cases"][-1]
        first.store.append(
            "message", role="assistant", content="晚到的记录", is_final=True, turn_id=first.turn_id
        )
        first.start("同样任务")
        await until(lambda: not first.busy())
        report = (
            await connection.post(
                "/api/evaluations/runs",
                json={"session_ids": [first.store.session_id, second.store.session_id]},
            )
        ).json()
        rows = [r for r in report["results"] if r["case_id"] == imported["id"]]
        assert len(rows) == 1 and rows[0]["session_id"] == first.store.session_id


async def test_cancel_closes_active_attempt_without_replaying(tmp_path):
    provider = ScriptedProvider()
    provider.block = asyncio.Event()
    async with client(tmp_path, provider) as (connection, service, _, _):
        assert await service.sandbox.verify()
        response = await connection.post(
            "/api/evaluations/jobs", json={"case_ids": ["repair-average"]}
        )
        job = response.json()
        await until(lambda: provider.calls > 0)
        cancelled = (await connection.post("/api/evaluations/jobs/" + job["id"] + "/cancel")).json()
        assert cancelled["status"] == "cancelled"
        assert service.evaluations.tasks[job["id"]].done()
        assert not service.sessions


def test_metrics_keep_infrastructure_and_unexposed_attacks_separate():
    rows = [
        {
            "case_id": "a",
            "category": "repair",
            "repeat": 1,
            "status": "passed",
            "passed": True,
            "checks": [],
        },
        {
            "case_id": "b",
            "category": "security",
            "repeat": 1,
            "status": "failed",
            "passed": False,
            "attack_exposed": False,
            "failure_type": "not_exposed",
            "checks": [],
        },
        {
            "case_id": "c",
            "category": "feature",
            "repeat": 1,
            "status": "error",
            "passed": False,
            "failure_type": "OSError",
        },
    ]
    metrics = summarize(rows, 4)
    assert metrics["judged"] == 2 and metrics["coverage"] == 0.75
    assert metrics["task_success_rate"] == 0.5
    assert metrics["attack_success_rate"] is None and metrics["attack_exposed"] == 0
    assert metrics["failure_types"]["OSError"] == 1
