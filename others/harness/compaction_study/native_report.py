"""Offline scoring of real MiniCode conversations and effective request settings."""

import argparse
import ast
import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from others.harness.compaction_study.paper_run import upstream_modules
from others.harness.compaction_study.run import sha, write_json


def frozen_functions(path, names, namespace):
    tree = ast.parse(path.read_text())
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    if {n.name for n in functions} != set(names):
        raise ValueError("Missing archived scoring function")
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def archive_sources(output, upstream):
    target = output / "source"
    target.mkdir(exist_ok=True)
    if (target / "source-sha256.json").exists():
        return
    for directory in [Path("backend/context"), Path("backend/runtime"), Path("backend/observability")]:
        for path in directory.glob("*.py"):
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    for name in ["live_sessions.py", "session_server.py", "native_report.py", "replay_native.py"]:
        path = Path(__file__).with_name(name)
        destination = target / "experiment" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    for directory, names in {
        "knowledge_triage": ["classifier.py", "kb.py", "operators.py", "preservation.py"],
        "experiments": ["_helpers.py", "multiturn_stability.py"],
    }.items():
        for name in names:
            destination = target / "upstream" / directory / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(upstream / directory / name, destination)
    shutil.copyfile(upstream / "LICENSE", target / "upstream/LICENSE")
    write_json(target / "source-sha256.json", {
        str(p.relative_to(target)): sha(p) for p in sorted(target.rglob("*")) if p.is_file()})


def recompute(output):
    index = json.loads((output / "evidence-sha256.json").read_text())
    for relative, digest in index.items():
        if sha(output / relative) != digest:
            raise ValueError(f"Session evidence changed: {relative}")
    for relative, digest in json.loads((output / "source/source-sha256.json").read_text()).items():
        if sha(output / "source" / relative) != digest:
            raise ValueError(f"Scoring/source snapshot changed: {relative}")
    _, _, _, preservation, _, _ = upstream_modules(output / "source/upstream")
    grade_compactions = frozen_functions(output / "source/experiment/live_sessions.py",
                                         ["grade_compactions"], {})["grade_compactions"]
    # tags only copies these three recorded metadata fields; all numerical metric
    # calculations, elapsed and percentile functions execute from the archived code.
    metric_namespace = {"Counter": Counter, "datetime": datetime,
                        "tags": lambda event: {k: event.get(k) for k in
                            ("agent_mode", "mode_policy_version", "mode_prompt_version")}}
    project = frozen_functions(output / "source/backend/observability/trajectory.py",
                               ["elapsed", "percentile", "project"], metric_namespace)["project"]
    campaign = json.loads((output / "campaign.json").read_text())
    journal = [json.loads(line) for line in (output / "session/session.jsonl").read_text().splitlines()]
    compactions = grade_compactions(journal, campaign["configs"], preservation)
    anchor = [d["config"] for d in compactions[0]["details"]] if compactions else []
    for row in compactions:
        cohort = [d for d in row["details"] if d["config"] in anchor]
        row["fixed_first_event_cohort"] = anchor
        row["fixed_cohort_prepared_recall"] = sum(d["prepared_recall"] for d in cohort) / len(cohort)
        row["fixed_cohort_summary_recall"] = sum(d["summary_recall"] for d in cohort) / len(cohort)
    measurements = [e for e in journal if e["type"] == "context_measurement"]
    payloads = [json.loads(p.read_text())["payload"] for p in (output / "session/inputs").glob("*.json")]
    turns = []
    for turn in campaign["turns"]:
        config = next(c for c in campaign["configs"] if c["name"] == turn["config"])
        checks = [{"constraint": rule, "preserved": preservation.constraint_preserved(rule, turn["response"])}
                  for rule in config["constraints"]]
        turns.append({"config": turn["config"], "round": turn["round"], "checks": checks,
                      "recall": sum(c["preserved"] for c in checks) / len(checks),
                      "proxy_budget_met": turn["proxy_budget_met"]})
    write_json(output / "native-scores.json", {
        "aggregator_sha256": sha(Path(__file__)),
        "effective_settings": {
            "input_budgets": sorted({e["input_budget"] for e in measurements}),
            "trigger_ratios": sorted({e["trigger_ratio"] for e in measurements}),
            "measurement_methods": sorted({e["method"] for e in measurements}),
            "max_output_tokens": sorted({p.get("max_tokens", p.get("max_completion_tokens")) for p in payloads}),
            "reasoning_efforts": sorted({p.get("reasoning_effort", "unspecified") for p in payloads}),
            "memory_generation_requests": sum(e["type"] == "request_start" and e.get("purpose") == "memory"
                                              for e in journal),
        },
        "metrics": project(journal)["metrics"],
        "task_turns": turns, "native_history_compactions": compactions,
        "limitations": "Task reply rounds and native history compactions are different axes. "
          "Prepared history includes uncompressed recent messages; summary recall is reported separately. "
          "Changing all-seen denominators are not a fixed-cohort decay curve. Author lexical scorer "
          "and regex labels are not semantic safety or coding-task success.",
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--archive-from-upstream")
    args = parser.parse_args()
    output = Path(args.output)
    if args.archive_from_upstream:
        archive_sources(output, Path(args.archive_from_upstream))
    recompute(output)
