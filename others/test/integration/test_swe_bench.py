"""Adapter regression tests; these are not SWE-bench model scores."""

import json
import shutil
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.evaluation.swe_bench.attempt import export_patch, git, prepare
from backend.evaluation.swe_bench.catalog import Catalog
from backend.evaluation.swe_bench.environment import DATASETS, Environment
from backend.evaluation.swe_bench.grader import grade, metrics
from backend.evaluation.swe_bench.jobs import RunInput, SweJobs
from backend.evaluation.swe_bench.worker import catalog, official_image
from backend.permissions.directories import SessionPermissions


def test_official_selection_rejects_other_dataset_and_duplicate_ids(tmp_path):
    env = Environment(tmp_path)
    source = Catalog(env)
    source.path.write_text(
        json.dumps(
            {
                "revision": "fixed",
                "digest": "digest",
                "datasets": {
                    DATASETS["swe_lite"]: [{"instance_id": "one"}],
                    DATASETS["swe_verified"]: [{"instance_id": "two"}],
                },
            }
        )
    )
    assert source.select("swe_lite", ["one"])[1][0]["instance_id"] == "one"
    for ids in (["two"], ["one", "one"], ["../../escape"]):
        with pytest.raises(ValueError):
            source.select("swe_lite", ids)


def test_public_issue_projection_never_exposes_gold_or_tests(monkeypatch):
    import sys
    import types

    task = {
        "instance_id": "one",
        "repo": "a/b",
        "base_commit": "a" * 40,
        "problem_statement": "Fix this",
        "version": "1",
        "patch": "PRIVATE GOLD",
        "test_patch": "PRIVATE TEST",
        "FAIL_TO_PASS": ["secret_test"],
        "eval_script": "private",
    }
    module = types.ModuleType("swebench.task.repo")
    module.published_tasks = lambda repo, datasets: {datasets[0]: {"test": [task], "dev": [task]}}
    monkeypatch.setitem(sys.modules, "swebench.task.repo", module)
    value = catalog("unused")
    assert list(value.values())[0] == [
        {k: task[k] for k in ("instance_id", "repo", "base_commit", "problem_statement", "version")}
    ]
    assert "PRIVATE" not in json.dumps(value)


@pytest.mark.asyncio
async def test_patch_exports_new_files_and_preserves_tracked_prompt(tmp_path):
    env = Environment(tmp_path)
    work = tmp_path / "candidate"
    work.mkdir()
    await git(env, work, "init", "--quiet")
    (work / "existing.py").write_text("x = 1\n")
    (work / "project_system_prompt.md").write_text("tracked prompt\n")
    await git(env, work, "add", ".")
    await git(
        env,
        work,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "initial",
    )
    base = (await git(env, work, "rev-parse", "HEAD")).strip()
    (work / "project_system_prompt.md").write_text("changed tracked prompt\n")
    (work / "new.py").write_text("x = 2\n")
    (work / "project_skills").mkdir()
    (work / "project_skills" / "generated.md").write_text("runtime scaffold")
    patch = await export_patch(env, work, base)
    assert "new.py" in patch and "+x = 2" in patch
    assert "+changed tracked prompt" in patch
    assert "generated.md" not in patch


def test_environment_errors_do_not_become_official_verdicts():
    result = metrics(
        [
            {"status": "resolved", "resolved": True},
            {"status": "environment_error"},
            {"status": "empty_patch"},
        ],
        4,
    )
    assert result["resolved_rate"] == 1
    assert result["coverage"] == 0.25 and result["judged"] == 1
    assert result["environment_errors"] == 1
    assert metrics([], 1)["resolved_rate"] is None


def test_official_image_missing_on_arm_engine_is_pulled_as_amd64(monkeypatch):
    import sys

    class MissingImage(Exception):
        pass

    image = SimpleNamespace(id="sha256:pinned", attrs={"Architecture": "amd64"})
    pulls = []

    class Images:
        def get(self, name):
            raise MissingImage()

        def pull(self, name, *, platform):
            pulls.append((name, platform))
            return image

    monkeypatch.setitem(
        sys.modules, "docker", SimpleNamespace(errors=SimpleNamespace(ImageNotFound=MissingImage))
    )
    assert official_image(SimpleNamespace(images=Images()), "official:latest") is image
    assert pulls == [("official:latest", "linux/amd64")]
    image.attrs["Architecture"] = "arm64"
    with pytest.raises(ValueError, match="linux/amd64"):
        official_image(SimpleNamespace(images=Images()), "official:latest")


def test_regrade_preserves_patch_and_original_usage_without_new_model_run(tmp_path):
    jobs = SweJobs.__new__(SweJobs)
    jobs.directory = tmp_path
    identifier = "a" * 32
    directory = tmp_path / identifier
    artifact = directory / "0"
    artifact.mkdir(parents=True)
    patch = "actual frozen patch\n"
    (artifact / "model.patch").write_text(patch)
    (artifact / "trace.json").write_text('{"events":[]}')
    (directory / "job.json").write_text(
        json.dumps(
            {
                "environment": {"backend_code_digest": "source"},
                "results": [
                    {
                        "instance_id": "one",
                        "artifact_index": 0,
                        "usage": {"requests": 18},
                        "agent_status": "paused",
                        "agent_duration_ms": 140219,
                    }
                ],
            }
        )
    )
    destination = tmp_path / "regrade"
    destination.mkdir()
    execution, actual = jobs.reuse(identifier, {"instance_id": "one"}, destination)
    assert actual == patch == (destination / "model.patch").read_text()
    assert execution["new_model_requests"] == 0 and execution["agent_duration_ms"] == 0
    assert execution["source_execution"]["usage"] == {"requests": 18}
    assert execution["source_backend_code_digest"] == "source"


def test_plan_cannot_be_used_as_coding_benchmark_and_limits_are_bounded():
    with pytest.raises(ValidationError):
        RunInput(dataset_id="swe_lite", instance_ids=["one"], agent_mode="plan")
    with pytest.raises(ValidationError):
        RunInput(dataset_id="swe_lite", instance_ids=[str(i) for i in range(11)])


def test_harness_processes_do_not_inherit_model_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-for-grader")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "also-not-for-grader")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-for-grader")
    env = Environment(tmp_path).variables()
    assert not any("KEY" in key or "TOKEN" in key for key in env)


def test_frozen_acceptance_tree_is_independent_of_later_edits(tmp_path):
    env = Environment(tmp_path)
    task = env.repository / "tasks" / "one"
    task.mkdir(parents=True)
    (env.repository / "sweb.yaml").write_text("datasets: [official]\n")
    (task / "test.patch").write_text("original acceptance")
    destination = tmp_path / "frozen"
    fingerprint = Catalog(env).freeze([{"instance_id": "one"}], destination)
    (task / "test.patch").write_text("changed acceptance")
    assert (destination / "tasks" / "one" / "test.patch").read_text() == "original acceptance"
    assert len(fingerprint) == 64


@pytest.mark.asyncio
async def test_candidate_can_be_authorized_without_exposing_internal_artifacts(tmp_path):
    env = Environment(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    await git(env, source, "init", "--quiet")
    (source / "solution.py").write_text("x = 1\n")
    await git(env, source, "add", ".")
    await git(
        env,
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "baseline",
    )
    base = (await git(env, source, "rev-parse", "HEAD")).strip()
    cache = env.directory / "repositories" / "a--b" / base
    cache.parent.mkdir(parents=True)
    shutil.move(source, cache)
    artifact = env.directory / "jobs" / "one"
    artifact.mkdir(parents=True)
    work = await prepare(env, {"repo": "a/b", "base_commit": base}, artifact, None, "one")
    try:
        store = SimpleNamespace(events=[], append=lambda *args, **kwargs: {"seq": 1})
        permissions = SessionPermissions(tmp_path, store)
        assert permissions.add(str(work)) == work
        assert not work.is_relative_to(tmp_path)
        assert (work / "solution.py").read_text() == "x = 1\n"
        assert permissions.roots() == (work,)
    finally:
        shutil.rmtree(work.parent)


@pytest.mark.asyncio
async def test_reads_official_v5_report_instead_of_inferring_from_process_exit(tmp_path):
    # Fixture follows swebench 5.0.2's RUN_EVALUATION_LOG_DIR contract.
    directory = tmp_path / "logs/run_evaluation/run/minicode/one"
    directory.mkdir(parents=True)
    verdict = {"one": {"resolved": False, "patch_successfully_applied": True}}
    (directory / "report.json").write_text(json.dumps(verdict))
    (tmp_path / "image.json").write_text(json.dumps({"repo_digests": ["sha256:fixture"]}))

    async def logged(*args):
        return None

    env = SimpleNamespace(logged=logged)
    result = await grade(env, {"instance_id": "one"}, tmp_path, None, "run", 30)
    assert result["resolved"] is False and result["official_report"] == verdict
    (directory / "report.json").unlink()
    with pytest.raises(ValueError, match="未生成单题报告"):
        await grade(env, {"instance_id": "one"}, tmp_path, None, "run", 30)
