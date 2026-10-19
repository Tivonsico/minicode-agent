"""Bounded asynchronous benchmark jobs, durable progress and frozen reports."""

import asyncio
import copy
import hashlib
import json
import platform
import uuid
from pathlib import Path

from backend.context.project_assets import ProjectAssets
from backend.conversation.store import atomic_json, internal_directory, now
from backend.execution.python_runtime import python_runtime
from backend.models.reasoning import validate
from backend.modes.catalog import compatible
from backend.modes.settings import ModeSettings

from .attempt import execute
from .metrics import summarize


class BenchmarkJobs:
    def __init__(self, service, repository):
        self.service, self.repository = service, repository
        self.directory = internal_directory(service.config.root, "evaluation", "jobs")
        self.artifacts = internal_directory(service.config.root, "evaluation", "attempts")
        self.tasks = {}
        for path in self.directory.glob("*.json"):
            if path.is_symlink():
                continue
            job = json.loads(path.read_text())
            if job["status"] == "running":
                job.update(status="interrupted", ended_at=now(), current=None)
                self._save(job)

    def _save(self, job):
        job["metrics"] = summarize(job["results"], job["planned"])
        atomic_json(self.directory / f"{job['id']}.json", job)

    def get(self, identifier):
        if (
            not isinstance(identifier, str)
            or len(identifier) != 32
            or any(c not in "0123456789abcdef" for c in identifier)
        ):
            raise ValueError("评测运行ID无效")
        path = self.directory / f"{identifier}.json"
        if path.is_symlink():
            raise ValueError("评测运行路径无效")
        return json.loads(path.read_text())

    def list(self):
        return sorted(
            [
                {
                    k: self.get(path.stem)[k]
                    for k in ("id", "status", "created_at", "current", "planned", "metrics")
                }
                for path in self.directory.glob("*.json")
                if not path.is_symlink()
            ],
            key=lambda j: j["created_at"],
            reverse=True,
        )

    def start(self, options):
        if any(not task.done() for task in self.tasks.values()):
            raise ValueError("已有评测在运行，请等待或停止后再启动")
        if not self.service.sandbox.available():
            raise ValueError("运行任务与独立验收需要通过系统沙箱验证；请先检查执行环境")
        options = options.model_copy(
            update={"reasoning_effort": validate(self.service.config, options.reasoning_effort)}
        )
        dataset = self.repository.dataset()
        selected = set(options.case_ids)
        cases = [case for case in dataset["cases"] if case["id"] in selected]
        if len(cases) != len(selected) or any(case["mode"] != "task" for case in cases):
            raise ValueError("请选择已补齐初始文件与验收条件的可运行任务")
        pairs = [
            (case, mode) for case in cases for mode in options.agent_modes if compatible(case, mode)
        ]
        if any(not any(pair[0]["id"] == case["id"] for pair in pairs) for case in cases):
            raise ValueError("实现任务需要执行模式，规划任务需要 Plan；请检查任务目标与所选模式")
        if any(not any(pair[1] == mode for pair in pairs) for mode in options.agent_modes):
            raise ValueError("所选模式没有对应评测目标；Plan 需要单独的规划任务和回复验收条件")
        config = self.service.config
        job = {
            "schema_version": 1,
            "kind": "agent_benchmark",
            "id": uuid.uuid4().hex,
            "status": "running",
            "created_at": now(),
            "current": None,
            "results": [],
            "dataset_name": dataset["name"],
            "dataset_revision": dataset["revision"],
            "dataset": copy.deepcopy(dataset),
            "dataset_digest": hashlib.sha256(
                json.dumps(dataset, sort_keys=True).encode()
            ).hexdigest(),
            "options": options.model_dump(),
            "planned": len(pairs) * options.repeats,
            "global_prompt": ProjectAssets(config.root).global_text(),
            "mode_profiles": {
                mode: ModeSettings(config.root).profile(mode) for mode in options.agent_modes
            },
            "environment": {
                "model": config.model,
                "reasoning_effort": options.reasoning_effort,
                "context_window": config.context_window,
                "max_output_tokens": config.max_output_tokens,
                "platform": platform.platform(),
                "python": platform.python_version(),
                "sandbox_verified": True,
                "memory": "isolated_disabled",
                "approval": "scripted_workspace_only:" + options.approval_behavior,
                "agent_modes": options.agent_modes,
                "task_delivery": "initial_file_manifest_and_requirements",
            },
        }
        job["job_id"] = job["id"]
        runtime = python_runtime(config.root)
        job["environment"]["python_executable"] = str(runtime[1]) if runtime else None
        files = [
            Path(__file__).with_name(name)
            for name in (
                "attempt.py",
                "task_input.py",
                "failure.py",
                "verifier.py",
                "worker.py",
                "metrics.py",
                "planning.py",
            )
        ]
        harness = Path(__file__).parents[1]
        files += [
            harness / name
            for name in (
                "runtime/loop.py",
                "context/system_prompt.py",
                "tools/registry.py",
                "execution/policy.py",
                "execution/python_runtime.py",
                "execution/codex.py",
                "tools/executor.py",
                "context/prompts.py",
                "modes/catalog.py",
                "modes/settings.py",
                "permissions/policy.py",
            )
        ]
        job["environment"]["harness_digest"] = hashlib.sha256(
            b"".join(path.read_bytes() for path in files)
        ).hexdigest()
        self._save(job)
        self.tasks[job["id"]] = asyncio.create_task(self._run(job, cases, options))
        return job

    async def _run(self, job, cases, options):
        try:
            for repeat in range(1, options.repeats + 1):
                for case in cases:
                    for mode in options.agent_modes:
                        if not compatible(case, mode):
                            continue
                        job["current"] = {
                            "case_id": case["id"],
                            "name": case["name"],
                            "repeat": repeat,
                            "agent_mode": mode,
                        }
                        self._save(job)
                        artifact = self.artifacts / (job["id"] + f"-{len(job['results']) + 1}")
                        row = await execute(
                            self.service,
                            case,
                            options,
                            repeat,
                            artifact,
                            mode_profile=job["mode_profiles"][mode],
                            global_prompt=job["global_prompt"],
                        )
                        job["results"].append(row)
                        self._save(job)
                        if row["status"] == "cancelled":
                            raise asyncio.CancelledError
            job["status"] = "completed"
        except asyncio.CancelledError:
            job["status"] = "cancelled"
        except Exception as exc:
            job.update(status="failed", error=type(exc).__name__)
        finally:
            job.update(current=None, ended_at=now())
            self._save(job)
            report = self.repository.save_report(job)
            job["report_id"] = report["id"]
            self._save(job)

    async def cancel(self, identifier):
        task = self.tasks.get(identifier)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                job = self.get(identifier)
                job.update(status="cancelled", current=None, ended_at=now())
                self._save(job)
        return self.get(identifier)

    async def close(self):
        for identifier in list(self.tasks):
            await self.cancel(identifier)

    def artifact(self, identifier, index):
        job = self.get(identifier)
        if not 0 <= index < len(job["results"]):
            raise ValueError("评测样本不存在")
        path = self.artifacts / job["results"][index]["artifact_ref"] / "trace.json"
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("评测样本路径无效")
        return json.loads(path.read_text())

    def input(self, identifier, index, ref):
        import re

        job = self.get(identifier)
        if not 0 <= index < len(job["results"]) or not re.fullmatch(
            r"inputs/[a-f0-9]{32}\.json", ref
        ):
            raise ValueError("请求输入引用无效")
        parent = self.artifacts / job["results"][index]["artifact_ref"]
        path = parent / ref
        if (
            path.is_symlink()
            or parent.is_symlink()
            or not path.resolve().is_relative_to(parent.resolve())
        ):
            raise ValueError("请求输入路径无效")
        return json.loads(path.read_text())
