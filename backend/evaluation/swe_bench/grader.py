"""Official verdicts only; an environment failure is not a resolved Issue."""

import json
import time


def result_directory(artifact, run_id, instance_id):
    return artifact / "logs" / "run_evaluation" / run_id / "minicode" / instance_id


async def grade(env, task, artifact, frozen_repository, run_id, timeout):
    started = time.monotonic()
    await env.logged(
        "grade", [frozen_repository, task["instance_id"], run_id, timeout], artifact, timeout + 3600
    )
    report = result_directory(artifact, run_id, task["instance_id"]) / "report.json"
    if not report.is_file():
        raise ValueError("官方验收未生成单题报告，请检查 Docker 环境与验收日志")
    result = json.loads(report.read_text())
    verdict = result.get(task["instance_id"], {})
    if not isinstance(verdict.get("resolved"), bool):
        raise ValueError("官方报告缺少 resolved 判定，不能统计成功率")
    return {
        "resolved": verdict["resolved"],
        "official_report": result,
        "grading_duration_ms": round((time.monotonic() - started) * 1000),
        "image": json.loads((artifact / "image.json").read_text()),
    }


def metrics(results, planned):
    judged = [row for row in results if isinstance(row.get("resolved"), bool)]
    resolved = sum(row["resolved"] for row in judged)
    return {
        "planned": planned,
        "finished": len(results),
        "judged": len(judged),
        "resolved": resolved,
        "resolved_rate": resolved / len(judged) if judged else None,
        "coverage": len(judged) / planned if planned else 0,
        "environment_errors": sum(row["status"] == "environment_error" for row in results),
        "unresolved": len(judged) - resolved,
    }
