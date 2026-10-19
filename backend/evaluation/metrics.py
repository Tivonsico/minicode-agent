"""Task outcomes, sample coverage and uncertainty with explicit denominators."""

import math
from collections import Counter

from backend.observability.trajectory import percentile


def rate(values):
    return sum(values) / len(values) if values else None


def wilson(successes, count):
    if not count:
        return None
    z = 1.96
    p = successes / count
    center = (p + z * z / (2 * count)) / (1 + z * z / count)
    radius = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / (1 + z * z / count)
    return [max(0, center - radius), min(1, center + radius)]


def summarize(results, planned, *, grouped_metrics=True):
    judged = [r for r in results if r["status"] in {"passed", "failed"}]
    coding = [r for r in judged if r.get("goal", "implementation") == "implementation"]
    planning = [r for r in judged if r.get("goal") == "planning"]
    security = [r for r in judged if r["category"] == "security" and r.get("attack_exposed")]
    regressions = [
        c["passed"] for r in judged for c in r.get("checks", []) if c["phase"] == "regression"
    ]
    first = [r for r in judged if r["repeat"] == 1]
    grouped = {}
    for row in judged:
        grouped.setdefault((row["case_id"], row.get("agent_mode", "legacy_unknown")), []).append(
            row["passed"]
        )
    failure_types = Counter(
        r.get("failure_type", "unknown") for r in results if not r.get("passed")
    )
    metrics = {
        "planned": planned,
        "finished": len(results),
        "judged": len(judged),
        "coverage": len(results) / planned if planned else None,
        "passed": sum(r["passed"] for r in judged),
        "task_success_rate": rate([r["passed"] for r in coding]),
        "planning_success_rate": rate([r["passed"] for r in planning]),
        "acceptance_success_rate": rate([r["passed"] for r in judged]),
        "implementation_judged": len(coding),
        "planning_judged": len(planning),
        "overall_success_rate": rate([r["passed"] for r in results]),
        "task_success_ci95": wilson(sum(r["passed"] for r in coding), len(coding)),
        "pass_at_1": rate([r["passed"] for r in first]),
        "any_success_rate": rate([any(v) for v in grouped.values()]),
        "regression_pass_rate": rate(regressions),
        "attack_exposed": len(security),
        "attack_success_rate": rate([r["attack_success"] for r in security]),
        "security_utility_rate": rate([r["functional_passed"] for r in security]),
        "category_rates": {
            c: rate([r["passed"] for r in judged if r["category"] == c])
            for c in sorted({r["category"] for r in judged})
        },
        "duration_p50_ms": percentile([r.get("duration_ms") for r in judged], 0.5),
        "duration_p95_ms": percentile([r.get("duration_ms") for r in judged], 0.95),
        "first_text_p50_ms": percentile(
            [v for r in judged for v in r.get("request_first_text_ms", [])], 0.5
        ),
        "known_tokens": sum(r.get("known_tokens", 0) for r in results),
        "unknown_usage_requests": sum(r.get("unknown_usage_requests", 0) for r in results),
        "requests": sum(r.get("requests", 0) for r in results),
        "retries": sum(r.get("retries", 0) for r in results),
        "tool_failures": sum(r.get("tool_failures", 0) for r in results),
        "failure_types": dict(failure_types),
        "approvals": sum(r.get("approvals", 0) for r in results),
        "cost": None,
    }

    if grouped_metrics:
        for field, key in (("agent_mode", "mode_metrics"), ("goal", "goal_metrics")):
            metrics[key] = {
                value: summarize(
                    [r for r in results if (r.get(field) or "legacy_unknown") == value],
                    None,
                    grouped_metrics=False,
                )
                for value in sorted({r.get(field) or "legacy_unknown" for r in results})
            }
    return metrics
