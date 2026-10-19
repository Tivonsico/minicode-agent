"""Evaluate frozen existing turns; this module never calls models or tools."""

from backend.modes.catalog import tags
from backend.observability.trajectory import percentile, project


def evaluate(dataset, sessions):
    results, turns, requests, sources = [], [], [], []
    totals = {
        "known_tokens": 0,
        "unknown_usage_requests": 0,
        "requests": 0,
        "retries": 0,
        "tool_calls": 0,
        "tool_failures": 0,
    }
    failures = {}
    for session in sessions:
        events = list(session.store.events)
        projection = project(events)
        sid = session.store.session_id
        sources.append({"session_id": sid, "covered_seq": events[-1]["seq"] if events else 0})
        turns.extend(projection["turns"])
        requests.extend(projection["requests"])
        for key in totals:
            totals[key] += projection["metrics"][key]
        for key, value in projection["metrics"]["failure_types"].items():
            failures[key] = failures.get(key, 0) + value
        for turn in projection["turns"]:
            if turn["status"] == "running":
                continue
            history = [e for e in events if e.get("turn_id") == turn["turn_id"]]
            for case in dataset["cases"]:
                if case.get("mode", "history") != "history":
                    continue
                if case.get("agent_mode") and case["agent_mode"] != turn.get("agent_mode"):
                    continue
                source = case.get("source", {})
                if source.get("type") == "session":
                    if source.get("session_id") != sid:
                        continue
                    observed = [e for e in history if e["seq"] <= source.get("covered_seq", 0)]
                    if not observed:
                        continue
                else:
                    observed = history
                if not all(text in turn.get("objective", "") for text in case["input_contains"]):
                    continue
                output = "\n".join(
                    e["content"]
                    for e in observed
                    if e["type"] == "message" and e.get("role") == "assistant" and e.get("is_final")
                )
                tools = {
                    e.get("name")
                    for e in observed
                    if e["type"] == "tool_result" and e.get("status") == "succeeded"
                }
                used_tools = {e.get("name") for e in observed if e["type"] == "tool_result"}
                verified = any(
                    e["type"] == "verification_result"
                    and e.get("status") == "passed"
                    and e.get("source") == "execution"
                    for e in observed
                )
                checks = []
                for text in case["output_contains"]:
                    checks.append({"name": f"回复包含：{text}", "passed": text in output})
                for tool in case["required_tools"]:
                    checks.append({"name": f"成功执行：{tool}", "passed": tool in tools})
                for tool in case["forbidden_tools"]:
                    checks.append({"name": f"未调用：{tool}", "passed": tool not in used_tools})
                if case["expected_status"]:
                    checks.append(
                        {
                            "name": f"执行状态：{case['expected_status']}",
                            "passed": turn["status"] == case["expected_status"],
                        }
                    )
                if case["require_verification"]:
                    checks.append({"name": "有通过的实际验证记录", "passed": verified})
                results.append(
                    {
                        **tags(turn),
                        "session_id": sid,
                        "turn_id": turn["turn_id"],
                        "run_id": turn.get("run_id"),
                        "case_id": case["id"],
                        "case_name": case["name"],
                        "checks": checks,
                        "passed": all(c["passed"] for c in checks),
                    }
                )
    passed = sum(r["passed"] for r in results)
    matched = {r["case_id"] for r in results}
    history_cases = [case for case in dataset["cases"] if case.get("mode", "history") == "history"]
    return {
        "schema_version": 1,
        "kind": "retrospective_rules",
        "dataset_name": dataset["name"],
        "dataset_revision": dataset["revision"],
        "dataset": dataset,
        "sources": sources,
        "results": results,
        "unmatched_cases": [c["name"] for c in history_cases if c["id"] not in matched],
        "metrics": {
            **totals,
            "evaluated": len(results),
            "passed": passed,
            "rule_pass_rate": passed / len(results) if results else None,
            "mode_metrics": {
                mode: {
                    "evaluated": len(rows),
                    "passed": sum(r["passed"] for r in rows),
                    "rule_pass_rate": sum(r["passed"] for r in rows) / len(rows),
                }
                for mode in sorted({r.get("agent_mode") or "legacy_unknown" for r in results})
                if (
                    rows := [
                        r for r in results if (r.get("agent_mode") or "legacy_unknown") == mode
                    ]
                )
            },
            "case_coverage": len(matched) / len(history_cases) if history_cases else None,
            "turn_duration_p50_ms": percentile([t.get("duration_ms") for t in turns], 0.5),
            "turn_duration_p95_ms": percentile([t.get("duration_ms") for t in turns], 0.95),
            "first_text_p50_ms": percentile([r.get("first_text_ms") for r in requests], 0.5),
            "failure_types": failures,
            "cost": None,
        },
    }
