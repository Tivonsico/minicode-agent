"""Aggregate all first attempts, keeping planned coverage and official verdicts."""

import json
from collections import Counter
from pathlib import Path

from backend.evaluation.swe_bench.grader import metrics

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / ".minicode/swe-bench"


def tests(row, group):
    verdict = row.get("official_report", {}).get(row["instance_id"], {})
    values = verdict.get("tests_status", {}).get(group)
    if values is None:
        return None
    passed = len(values.get("success", []))
    return {"passed": passed, "total": passed + len(values.get("failure", []))}


def count_tests(value):
    return f"{value['passed']}/{value['total']}" if value else "—"


def main():
    manifest = json.loads((ROOT / "docs/harness/swe-bench-25-manifest.json").read_text())
    campaign = json.loads((DIRECTORY / "campaign-25.json").read_text())
    baselines = {
        row["instance_id"]: row["image"]["image_id"]
        for row in json.loads((ROOT / "docs/harness/swe-bench-25-baseline-images.json").read_text())
    }
    results = []
    for identifier in campaign["job_ids"]:
        job = json.loads((DIRECTORY / "jobs" / identifier / "job.json").read_text())
        assert job["environment"]["backend_code_digest"] == campaign["backend_code_digest"]
        assert job["environment"]["native_profiles_digest"] == campaign["native_profiles_digest"]
        for row in job["results"]:
            artifact = DIRECTORY / "jobs" / identifier / str(row["artifact_index"])
            usage = {}
            trace = artifact / "trace.json"
            if trace.is_file():
                for event in json.loads(trace.read_text())["events"]:
                    if event["type"] == "usage" and isinstance(event.get("usage"), dict):
                        usage[event["request_id"]] = event["usage"]
            results.append(
                dict(
                    row,
                    job_id=identifier,
                    artifact=str(artifact),
                    fail_to_pass=tests(row, "FAIL_TO_PASS"),
                    pass_to_pass=tests(row, "PASS_TO_PASS"),
                    cumulative_input_tokens=sum(u.get("prompt_tokens", 0) for u in usage.values()),
                    cumulative_output_tokens=sum(
                        u.get("completion_tokens", 0) for u in usage.values()
                    ),
                    image_matches_baseline=(
                        row["image"]["image_id"] == baselines[row["instance_id"]]
                        if row.get("image")
                        else None
                    ),
                )
            )
    assert len({row["instance_id"] for row in results}) == len(results)
    by_id = {row["instance_id"]: row for row in results}
    planned = Counter(task["repo"] for task in manifest["tasks"])
    summary = metrics(results, 25)
    summary["resolved_of_planned"] = summary["resolved"] / 25
    report = {
        "phase": "first",
        "status": campaign["status"],
        "metrics": summary,
        "backend_code_digest": campaign["backend_code_digest"],
        "native_profiles_digest": campaign["native_profiles_digest"],
        "job_ids": campaign["job_ids"],
        "status_counts": dict(Counter(r["status"] for r in results)),
        "agent_status_counts": dict(Counter(r.get("agent_status", "not_started") for r in results)),
        "agent_end_reason_counts": dict(
            Counter(r.get("agent_end_reason") or "unknown" for r in results)
        ),
        "usage_totals": {
            key: sum(r.get("usage", {}).get(key, 0) for r in results)
            for key in (
                "requests", "tool_calls", "known_tokens", "unknown_usage_requests", "retries"
            )
        },
        "repositories": {
            repo: metrics([r for r in results if r["repo"] == repo], count)
            for repo, count in planned.items()
        },
        "results": [
            by_id[t["instance_id"]] for t in manifest["tasks"] if t["instance_id"] in by_id
        ],
        "pending_instance_ids": [
            t["instance_id"] for t in manifest["tasks"] if t["instance_id"] not in by_id
        ],
    }
    output = ROOT / "docs/harness/swe-bench-25-first-results"
    output.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    judged_rate = f"{summary['resolved_rate']:.1%}" if summary["judged"] else "暂无判定"
    lines = [
        "# 25 道新增 SWE-bench 官方任务：首次运行",
        "",
        f"状态：{campaign['status']}。已结束 {len(results)}/25 项。",
        f"通过/计划：{summary['resolved']}/25（{summary['resolved_of_planned']:.1%}）；"
        f"通过/已判定：{summary['resolved']}/{summary['judged']}（{judged_rate}）；"
        f"判定覆盖率：{summary['judged']}/25（{summary['coverage']:.1%}）。",
        "",
        "清单按固定公开元数据抽样，覆盖六个仓库；五道旧 SymPy 题已排除。"
        "此 25 题子集不能估计完整榜单成绩。正式首轮开始后未改动 Agent、依赖或预算。",
        "",
        f"后端摘要：`{campaign['backend_code_digest']}`。",
        f"原生配置摘要：`{campaign['native_profiles_digest']}`。",
        "GLM-5.3-Flash / auto / low / Agent 600 秒 / 官方每题测试 900 秒 / 30 请求。"
        "镜像下载与初始化另有运行器超时，Docker 总耗时可超过测试预算。",
        "",
        "首次报告 ID：" + "、".join(f"`{identifier}`" for identifier in campaign["job_ids"]) + "。",
        "前端：评测 → SWE-bench 官方规则 → 历史报告。可查看补丁、实际请求输入、轨迹和官方日志。",
        "",
        "结果分类：" + "、".join(f"{k} {v}" for k, v in report["status_counts"].items()) + "。",
        "结束原因："
        + "、".join(f"{k} {v}" for k, v in report["agent_end_reason_counts"].items())
        + "。执行状态与官方判定分别记录。",
        "",
        "|仓库|通过/计划|通过/已判定|判定覆盖|环境错误|",
        "|---|---|---|---|---|",
    ]
    for repo, value in report["repositories"].items():
        lines.append(
            f"|{repo}|{value['resolved']}/{value['planned']}|"
            f"{value['resolved']}/{value['judged']}|{value['coverage']:.1%}|{value['environment_errors']}|"
        )
    lines += [
        "",
        "|任务（链接为原始轨迹）|结果 / Agent 状态|修复测试|回归测试|请求 / 工具|记录到的累计 token|Agent / Docker 秒|结束原因或错误|",
        "|---|---|---|---|---|---|---|---|",
    ]
    for task in manifest["tasks"]:
        row = by_id.get(task["instance_id"])
        if row is None:
            lines.append(f"|{task['instance_id']}|尚未结束|—|—|—|—|—|—|")
            continue
        usage = row.get("usage", {})
        reason = row.get("error") or row.get("agent_error") or row.get("agent_end_reason") or "—"
        reason = str(reason).replace("|", "\\|").replace("\n", " ")
        trace_link = f"../../.minicode/swe-bench/jobs/{row['job_id']}/{row['artifact_index']}/trace.json"
        grading_time = (
            f"{row['grading_duration_ms'] / 1000:.3f}"
            if "grading_duration_ms" in row
            else "—"
        )
        lines.append(
            f"|[{row['instance_id']}]({trace_link})|{row['status']} / {row.get('agent_status', '—')}|"
            f"{count_tests(row['fail_to_pass'])}|{count_tests(row['pass_to_pass'])}|"
            f"{usage.get('requests', '—')} / {usage.get('tool_calls', '—')}|"
            f"{usage.get('known_tokens', '—')}|"
            f"{row.get('agent_duration_ms', 0) / 1000:.3f} / {grading_time}|{reason}|"
        )
    lines += [
        "",
        "记录到的累计输入 token："
        + str(sum(r["cumulative_input_tokens"] for r in results))
        + "；记录到的累计输出 token："
        + str(sum(r["cumulative_output_tokens"] for r in results))
        + "。"
        "这些是跨请求累计用量，不是单次上下文长度或费用。"
        + f"共 {report['usage_totals']['requests']} 次实际模型请求、"
        + f"{report['usage_totals']['tool_calls']} 次工具调用；"
        + f"{report['usage_totals']['unknown_usage_requests']} 次请求的用量未知，"
        + "因此累计 token 不代表完整消耗。Docker 时间为 — 的行没有成功验收耗时记录。",
        "",
        "完整逐题记录见同名 JSON；所有原始 job、失败日志和附件保留。修复后的复测另存。",
        "环境错误的原补丁恢复验收及开发组对照见 [诊断与修复](swe-bench-25-repairs.md)。"
        "它们没有计入或改写本表的首轮成绩。",
    ]
    output.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
