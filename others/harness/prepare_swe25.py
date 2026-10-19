"""Prepare each frozen task and native sandbox before the first model call."""

import asyncio
import json
import shutil
import sys
import time
import uuid
from pathlib import Path

from backend.evaluation.swe_bench.attempt import export_patch, prepare
from backend.evaluation.swe_bench.environment import Environment
from backend.evaluation.swe_bench.self_test import prepare as prepare_self_test
from backend.execution.codex import CodexSandbox

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs/harness/swe-bench-25-manifest.json"
DESTINATION = ROOT / ".minicode/swe-bench/preflight-25"


async def main():
    env = Environment(ROOT)
    tasks = json.loads(MANIFEST.read_text())["tasks"]
    force = "--force" in sys.argv
    indices = [value for value in sys.argv[1:] if value != "--force"]
    if indices:
        wanted = {int(value) for value in indices}
        tasks = [task for task in tasks if task["index"] in wanted]
    readiness = await env.readiness()
    if not readiness["ready"]:
        raise RuntimeError(readiness)
    DESTINATION.mkdir(exist_ok=True)
    sandbox = CodexSandbox(DESTINATION, lambda: ())
    if not await sandbox.verify():
        raise RuntimeError(sandbox.reason)
    for task in tasks:
        artifact = DESTINATION / str(task["index"])
        artifact.mkdir(exist_ok=True)
        result_path = artifact / "result.json"
        if (
            not force
            and result_path.is_file()
            and json.loads(result_path.read_text()).get("status") == "ready"
        ):
            continue
        if result_path.is_file():
            previous = artifact / "attempts" / uuid.uuid4().hex
            previous.mkdir(parents=True)
            for path in list(artifact.iterdir()):
                if path.name != "attempts":
                    shutil.move(path, previous / path.name)
        started = time.monotonic()
        run_id = uuid.uuid4().hex
        work = None
        result = {"instance_id": task["instance_id"], "status": "preparing"}
        try:
            work = await prepare(env, task, artifact, env.repository, run_id)
            result["self_test_environment"] = await prepare_self_test(
                env, task, work, artifact, sandbox
            )
            if (await export_patch(env, work, task["base_commit"])).strip():
                raise ValueError("Native package preparation changed candidate source")
            result["baseline_patch_empty"] = True
            result["status"] = "ready"
        except Exception as exc:
            result.update(
                status="environment_error", error=str(exc), failure_type=type(exc).__name__
            )
        finally:
            if work:
                shutil.rmtree(work.parent, ignore_errors=True)
            try:
                await env.command("cleanup", run_id, timeout=20)
            except Exception as exc:
                result["cleanup_error"] = str(exc)
            result["duration_ms"] = round((time.monotonic() - started) * 1000)
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(
                task["index"],
                task["instance_id"],
                result["status"],
                result["duration_ms"],
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(main())
