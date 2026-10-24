"""Give Minicode a clean repository and public Issue; export its actual patch."""

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from backend.context.project_assets import ProjectAssets
from backend.evaluation.attempt import EmptyMemory, settle
from backend.execution.process import capture
from backend.observability.trajectory import project
from backend.runtime.session import RuntimeSession


async def git(env, directory, *args, timeout=600):
    code, out, err = await capture(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "credential.helper=", *args],
        directory,
        env.variables(),
        timeout,
    )
    if code:
        raise ValueError("仓库准备或补丁导出失败：" + err[-1500:])
    return out


async def prepare(env, task, artifact, repository, run_id):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", task["repo"]) or not re.fullmatch(
        r"[a-f0-9]{40}", task["base_commit"]
    ):
        raise ValueError("官方仓库或版本格式无效")
    cache = env.directory / "repositories" / task["repo"].replace("/", "--") / task["base_commit"]
    if not (cache / ".git").is_dir():
        await env.logged(
            "prepare", [repository, task["instance_id"], cache, run_id], artifact, 3600
        )
    # Candidate code must not share the application's protected artifact tree.
    temporary = Path(tempfile.mkdtemp(prefix="minicode-swe-")).resolve()
    work = temporary / "workspace"
    try:
        await git(
            env, artifact, "clone", "--quiet", "--shared", "--no-checkout", str(cache), str(work)
        )
        await git(env, work, "checkout", "--quiet", "--detach", task["base_commit"])
        if (await git(env, work, "rev-parse", "HEAD")).strip() != task["base_commit"]:
            raise ValueError("准备的仓库版本与官方 base_commit 不一致")
    except BaseException:
        shutil.rmtree(temporary)
        raise
    return work


async def export_patch(env, work, base_commit):
    names = (await git(env, work, "ls-files", "--others", "--exclude-standard", "-z")).split("\0")
    # Runtime scaffolding is never included in model submissions. Existing tracked
    # files still participate in git diff, including a project's own prompt file.
    added = [
        name
        for name in names
        if name
        and name != "project_system_prompt.md"
        and not name.startswith(("project_skills/", ".minicode/"))
    ]
    for start in range(0, len(added), 100):
        await git(env, work, "add", "--intent-to-add", "--", *added[start : start + 100])
    return await git(env, work, "diff", "--binary", "--no-ext-diff", base_commit, "--")


async def execute(service, env, task, work, artifact, options, profile, global_prompt, self_test):
    started = time.monotonic()
    root = artifact / "runtime"
    root.mkdir()
    assets = ProjectAssets(root)
    assets.ensure_global()
    assets.global_path().write_text(global_prompt, encoding="utf-8")
    session = RuntimeSession(
        replace(service.config, root=root),
        "swebench",
        service.provider,
        EmptyMemory(),
        service.sandbox,
    )
    session.permissions.add(str(work))
    backend = session.executor.shell_backend
    backend.python_environment = Path(self_test["environment"])
    backend.python_paths = tuple(Path(p) for p in self_test.get("python_paths", []))
    backend.runtime_roots = (Path(self_test["environment"]), Path(self_test["base_runtime"]))
    status = "failed"
    error = None
    try:
        async with asyncio.timeout(options.agent_timeout_seconds):
            session.start(
                f"修复以下真实仓库 Issue。工作目录：{work}\n"
                f"仓库：{task['repo']}，初始版本：{task['base_commit']}。\n"
                "直接修改必要的源代码并尽可能自测；保留兼容性，避免无关修改。"
                f"自测环境已预检通过：Python {self_test['python']}，依赖已按此仓库版本固定。"
                "python、python3、pytest 默认使用已配置的隔离环境，无需安装或伪造依赖。"
                "可以运行仓库已有测试和问题复现。"
                "pytest 关闭第三方插件自动加载；需要时可用 -p 显式加载已安装的插件。"
                "当前自测环境是本机操作系统沙箱；最终补丁由独立官方 Docker 环境验收。\n\n"
                + task["problem_statement"],
                options.reasoning_effort,
                options.agent_mode,
                mode_profile=profile,
            )
            await settle(session, options.approval_behavior)
            status = session.loop.status
    except TimeoutError:
        status, error = "timeout", "Agent 超过本次时间上限"
    finally:
        await session.cancel()
        events = session.store.events
        projection = project(events)
        (artifact / "trace.json").write_text(
            json.dumps({"events": events, "projection": projection}, ensure_ascii=False),
            encoding="utf-8",
        )
        inputs = session.store.directory / "inputs"
        if inputs.is_dir():
            shutil.copytree(inputs, artifact / "inputs", dirs_exist_ok=True)
    patch = await export_patch(env, work, task["base_commit"])
    (artifact / "model.patch").write_text(patch, encoding="utf-8")
    return {
        "agent_status": status,
        "agent_error": error,
        "agent_end_reason": next(
            (e.get("reason") for e in reversed(events) if e["type"] == "turn_end"), None
        ),
        "patch_bytes": len(patch.encode()),
        "agent_duration_ms": round((time.monotonic() - started) * 1000),
        "usage": projection["metrics"],
        "session_id": session.store.session_id,
        "run_ids": [e["run_id"] for e in events if e["type"] == "run_start"],
    }, patch
