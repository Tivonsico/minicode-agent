"""Public, versioned official tasks; full acceptance data stays outside the Agent."""

import asyncio
import json
import shutil
import uuid

from backend.execution.process import capture
from backend.observability.inputs import digest
from backend.storage.files import atomic_json

from .environment import DATASETS, SOURCE


class Catalog:
    def __init__(self, environment):
        self.environment = environment
        self.path = environment.directory / "catalog.json"
        self.lock = asyncio.Lock()

    def read(self, dataset_id):
        if dataset_id not in DATASETS:
            raise ValueError("请选择 SWE-bench Lite 或 Verified 官方评测集")
        if not self.path.is_file():
            return {"dataset_id": dataset_id, "tasks": [], "source": SOURCE, "revision": None}
        value = json.loads(self.path.read_text())
        return {
            "dataset_id": dataset_id,
            "source": SOURCE,
            "revision": value["revision"],
            "tasks": value["datasets"][DATASETS[dataset_id]],
            "digest": value["digest"],
        }

    async def sync(self):
        async with self.lock:
            env = self.environment
            repository = env.repository
            if not (repository / ".git").is_dir():
                if repository.exists():
                    raise ValueError("任务目录不是完整官方仓库，请修复后再同步")
                staging = repository.with_name("tasks-" + uuid.uuid4().hex)
                try:
                    code, _, err = await capture(
                        [
                            "git",
                            "-c",
                            "http.version=HTTP/1.1",
                            "clone",
                            "--depth",
                            "1",
                            SOURCE,
                            str(staging),
                        ],
                        env.directory,
                        env.variables(),
                        1200,
                    )
                    if code:
                        raise ValueError("官方任务下载失败：" + err[-1000:])
                    staging.rename(repository)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            # Sync indexes the local pinned checkout; fetching a new revision is explicit.
            code, revision, err = await capture(
                ["git", "rev-parse", "HEAD"],
                repository,
                env.variables(),
                10,
            )
            if code:
                raise ValueError(err)
            code, dirty, err = await capture(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                repository,
                env.variables(),
                30,
            )
            if code or dirty:
                raise ValueError("官方任务仓库存在修改，不能作为固定评测来源")
            tasks = await env.command("catalog", repository, timeout=120)
            atomic_json(
                self.path,
                {"revision": revision.strip(), "datasets": tasks, "digest": digest(tasks)},
            )
            return {"revision": revision.strip(), "counts": {k: len(v) for k, v in tasks.items()}}

    def select(self, dataset_id, identifiers):
        catalog = self.read(dataset_id)
        tasks = {task["instance_id"]: task for task in catalog["tasks"]}
        if len(set(identifiers)) != len(identifiers) or any(i not in tasks for i in identifiers):
            raise ValueError("所选任务必须属于当前官方评测集的 test 分区，且不能重复")
        return catalog, [tasks[i] for i in identifiers]

    def freeze(self, tasks, destination):
        """A job has its own official tree; later catalog changes cannot change its tests."""
        destination.mkdir(parents=True)
        source = self.environment.repository
        shutil.copy2(source / "sweb.yaml", destination / "sweb.yaml")
        for task in tasks:
            directory = source / "tasks" / task["instance_id"]
            if directory.is_symlink() or any(p.is_symlink() for p in directory.rglob("*")):
                raise ValueError("官方任务数据不能包含符号链接")
            shutil.copytree(directory, destination / "tasks" / task["instance_id"])
        return digest(
            {
                p.relative_to(destination).as_posix(): digest(p.read_bytes().hex())
                for p in sorted(destination.rglob("*"))
                if p.is_file()
            }
        )
