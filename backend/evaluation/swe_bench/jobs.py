"""Durable official subset jobs, with separate execution and acceptance lifecycles."""

import asyncio
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from backend.context.project_assets import ProjectAssets
from backend.conversation.metadata import now
from backend.models.reasoning import validate
from backend.modes.catalog import tags
from backend.modes.settings import ModeSettings
from backend.observability.inputs import digest, redact
from backend.storage.files import atomic_json, internal_directory

from .attempt import execute, export_patch, prepare
from .catalog import Catalog
from .environment import DATASETS, VERSION, Environment
from .grader import grade, metrics, result_directory
from .self_test import prepare as prepare_self_test


class RunInput(BaseModel):
    dataset_id: Literal["swe_lite", "swe_verified"]
    instance_ids: list[str] = Field(min_length=1, max_length=10)
    agent_mode: Literal["manual", "accept_edit", "auto"] = "auto"
    approval_behavior: Literal["approve", "reject"] = "approve"
    reasoning_effort: str | None = None
    agent_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    grader_timeout_seconds: int = Field(default=1800, ge=30, le=3600)
    source_job_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class SweJobs:
    def __init__(self, service):
        self.service = service
        self.env = Environment(service.config.root)
        self.catalog = Catalog(self.env)
        self.directory = internal_directory(service.config.root, "swe-bench", "jobs")
        self.tasks = {}
        self.start_lock = asyncio.Lock()
        for path in self.directory.glob("*/job.json"):
            job = json.loads(path.read_text())
            if job["status"] == "running":
                if job.get("current"):
                    job["results"].append(
                        {**job["current"], "status": "interrupted", "ended_at": now()}
                    )
                job.update(status="interrupted", current=None, ended_at=now())
                self.save(job)

    def path(self, identifier):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("官方评测运行 ID 无效")
        directory = self.directory / identifier
        if directory.is_symlink():
            raise ValueError("官方评测运行路径无效")
        return directory

    def save(self, job):
        job["metrics"] = metrics(job["results"], job["planned"])
        atomic_json(self.path(job["id"]) / "job.json", redact(job, (self.service.config.api_key,)))

    def get(self, identifier):
        return json.loads((self.path(identifier) / "job.json").read_text())

    def list(self):
        return sorted(
            [self.get(p.parent.name) for p in self.directory.glob("*/job.json")],
            key=lambda job: job["created_at"],
            reverse=True,
        )

    async def start(self, options):
        async with self.start_lock:
            if any(not task.done() for task in self.tasks.values()):
                raise ValueError("已有官方评测在运行")
            if not options.source_job_id and not self.service.sandbox.available():
                raise ValueError("Agent 执行需要先通过操作系统沙箱验证")
            readiness = await self.env.readiness()
            if not readiness["ready"]:
                raise ValueError(
                    "官方评测环境尚未就绪：" + readiness.get("reason", "请安装环境并同步任务")
                )
            await self.catalog.sync()
            catalog, tasks = self.catalog.select(options.dataset_id, options.instance_ids)
            if options.source_job_id:
                source = self.get(options.source_job_id)
                available = {row["instance_id"]: row for row in source["results"]}
                if (
                    source["status"] == "running"
                    or source["task_revision"] != catalog["revision"]
                    or source["dataset_id"] != options.dataset_id
                    or any(identifier not in available for identifier in options.instance_ids)
                ):
                    raise ValueError("复用补丁必须来自已结束、同来源版本和数据集的任务")
            options = options.model_copy(
                update={"reasoning_effort": validate(self.service.config, options.reasoning_effort)}
            )
            identifier = uuid.uuid4().hex
            directory = self.path(identifier)
            directory.mkdir()
            profile = ModeSettings(self.service.config.root).profile(options.agent_mode)
            job = {
                "id": identifier,
                "kind": "swe_bench",
                "status": "running",
                "created_at": now(),
                "planned": len(tasks),
                "results": [],
                "current": None,
                "execution_reused": bool(options.source_job_id),
                "source_job_id": options.source_job_id,
                "options": options.model_dump(),
                "dataset_id": options.dataset_id,
                "official_dataset": DATASETS[options.dataset_id],
                "split": "test",
                "subset": True,
                "task_revision": catalog["revision"],
                "catalog_digest": catalog["digest"],
                "public_tasks": tasks,
                "frozen_task_digest": self.catalog.freeze(tasks, directory / "task-repo"),
                "environment": {
                    **readiness,
                    "harness_version": VERSION,
                    "model": self.service.config.model,
                    "agent_environment": "host_os_sandbox",
                    "grader_environment": "official_docker",
                    "memory": "isolated_disabled",
                    "native_profiles_digest": digest(
                        json.loads(Path(__file__).with_name("self_test_profiles.json").read_text())
                    ),
                    "backend_code_digest": digest(
                        {
                            str(path.relative_to(Path(__file__).parents[2])): digest(
                                path.read_bytes().hex()
                            )
                            for path in sorted(Path(__file__).parents[2].rglob("*.py"))
                        }
                    ),
                },
                "mode_profile": profile,
                "global_prompt": ProjectAssets(self.service.config.root).global_text(),
            }
            job["input_digest"] = digest([tasks, job["options"], profile, job["global_prompt"]])
            self.save(job)
            self.tasks[identifier] = asyncio.create_task(self.run(job, tasks, options))
            return job

    async def run(self, job, tasks, options):
        try:
            for index, task in enumerate(tasks):
                row = {
                    "instance_id": task["instance_id"],
                    "repo": task["repo"],
                    "base_commit": task["base_commit"],
                    **tags(job["mode_profile"]),
                    "status": "preparing",
                    "started_at": now(),
                    "artifact_index": index,
                    "official_run_id": uuid.uuid4().hex,
                }
                artifact = self.path(job["id"]) / str(index)
                artifact.mkdir()
                job["current"] = row
                self.save(job)
                work = None
                preparation_started = time.monotonic()
                try:
                    if job["execution_reused"]:
                        execution, patch = self.reuse(job["source_job_id"], task, artifact)
                        row["preparation_duration_ms"] = 0
                    else:
                        work = await prepare(
                            self.env,
                            task,
                            artifact,
                            self.path(job["id"]) / "task-repo",
                            row["official_run_id"],
                        )
                        row["self_test_environment"] = await prepare_self_test(
                            self.env, task, work, artifact, self.service.sandbox
                        )
                        if (await export_patch(self.env, work, task["base_commit"])).strip():
                            raise ValueError("原生环境准备改写了候选源码；未请求模型")
                        row["preparation_duration_ms"] = round(
                            (time.monotonic() - preparation_started) * 1000
                        )
                        row["status"] = "executing"
                        self.save(job)
                        execution, patch = await execute(
                            self.service,
                            self.env,
                            task,
                            work,
                            artifact,
                            options,
                            job["mode_profile"],
                            job["global_prompt"],
                            row["self_test_environment"],
                        )
                    row.update(execution)
                    prediction = {
                        "instance_id": task["instance_id"],
                        "model_name_or_path": "minicode",
                        "model_patch": patch,
                    }
                    (artifact / "predictions.jsonl").write_text(json.dumps(prediction) + "\n")
                    if not patch.strip():
                        row.update(status="empty_patch")
                    else:
                        row["status"] = "grading"
                        self.save(job)
                        row.update(
                            await grade(
                                self.env,
                                task,
                                artifact,
                                self.path(job["id"]) / "task-repo",
                                row["official_run_id"],
                                options.grader_timeout_seconds,
                            )
                        )
                        row["status"] = "resolved" if row["resolved"] else "unresolved"
                except asyncio.CancelledError:
                    row.update(status="cancelled")
                    raise
                except Exception as exc:
                    row.update(
                        failure_stage=row["status"],
                        status="environment_error"
                        if row["status"] in {"preparing", "grading"}
                        else "agent_error",
                        error=str(exc),
                        failure_type=type(exc).__name__,
                    )
                finally:
                    if "preparation_duration_ms" not in row:
                        row["preparation_duration_ms"] = round(
                            (time.monotonic() - preparation_started) * 1000
                        )
                    if work is not None:
                        shutil.rmtree(work.parent, ignore_errors=True)
                    try:
                        await asyncio.shield(
                            self.env.command("cleanup", row["official_run_id"], timeout=20)
                        )
                        row["container_cleanup"] = "confirmed"
                    except (OSError, ValueError, TimeoutError):
                        row["container_cleanup"] = "unknown"
                    row["ended_at"] = now()
                    job["results"].append(row.copy())
                    job["current"] = None
                    self.save(job)
            job["status"] = "completed"
        except asyncio.CancelledError:
            job["status"] = "cancelled"
        except Exception as exc:
            job.update(status="failed", error=str(exc))
        finally:
            job.update(current=None, ended_at=now())
            self.save(job)

    def reuse(self, source_id, task, artifact):
        """Copy genuine evidence; new acceptance does not bill a new model run."""
        source = self.get(source_id)
        row = next(r for r in source["results"] if r["instance_id"] == task["instance_id"])
        directory = self.path(source_id) / str(row["artifact_index"])
        patch = (directory / "model.patch").read_text()
        for name in ("model.patch", "trace.json", "inputs"):
            path = directory / name
            if path.is_dir():
                shutil.copytree(path, artifact / name)
            elif path.is_file():
                shutil.copy2(path, artifact / name)
        return {
            "execution_reused": True,
            "source_job_id": source_id,
            "source_artifact_index": row["artifact_index"],
            "source_backend_code_digest": source["environment"]["backend_code_digest"],
            "source_execution": {
                key: row.get(key)
                for key in (
                    "agent_status",
                    "agent_end_reason",
                    "agent_duration_ms",
                    "usage",
                    "self_test_environment",
                )
            },
            "agent_status": "reused",
            "agent_end_reason": "execution_reused",
            "agent_duration_ms": 0,
            "new_model_requests": 0,
            "patch_bytes": len(patch.encode()),
        }, patch

    async def cancel(self, identifier):
        task = self.tasks.get(identifier)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                job = self.get(identifier)
                job.update(status="cancelled", ended_at=now(), current=None)
                self.save(job)
        return self.get(identifier)

    async def close(self):
        for identifier in self.tasks:
            await self.cancel(identifier)

    def artifact(self, identifier, index, name):
        job = self.get(identifier)
        if not 0 <= index < len(job["results"]):
            raise ValueError("请选择已结束的官方任务")
        directory = self.path(identifier) / str(index)
        if name == "trace":
            path = directory / "trace.json"
            return json.loads(path.read_text()) if path.is_file() else {"events": []}
        if name == "patch":
            path = directory / "model.patch"
        elif name == "log":
            row = job["results"][index]
            official = result_directory(directory, row["official_run_id"], row["instance_id"])
            return {
                "text": "\n\n".join(
                    f"=== {path.name} ===\n" + path.read_text(errors="replace")[-250000:]
                    for path in (
                        directory / "prepare.log",
                        directory / "dependency-install.log",
                        directory / "self-test-preflight.log",
                        directory / "grade.log",
                        official / "run_instance.log",
                    )
                    if path.is_file()
                )
                or "暂无此附件"
            }
        elif name == "tests":
            run_id = job["results"][index]["official_run_id"]
            path = (
                result_directory(directory, run_id, job["results"][index]["instance_id"])
                / "test_output.txt"
            )
        else:
            raise ValueError("验收附件类型无效")
        return {
            "text": path.read_text(errors="replace")[-500000:] if path.is_file() else "暂无此附件"
        }

    def input(self, identifier, index, request_id):
        job = self.get(identifier)
        if not 0 <= index < len(job["results"]) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("实际请求输入引用无效")
        path = self.path(identifier) / str(index) / "inputs" / (request_id + ".json")
        if path.is_symlink():
            raise ValueError("实际请求输入路径无效")
        return json.loads(path.read_text())
