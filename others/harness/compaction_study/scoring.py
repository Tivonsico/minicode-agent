"""Deterministic closed-set retrieval scoring, not an unrestricted factuality judge."""

import json
import re


def parse_json(text):
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped)
    return json.loads(stripped)


def normalize(value):
    return str(value).strip().strip("`\"'").rstrip(".")


def score(case, round_number, answers):
    truth = {p["id"]: p["expected"] for p in case["probes"]}
    changed = set()
    for r in case["rounds"][:round_number]:
        truth.update(r["updates"])
        changed.update(r["updates"])
    details = []
    for p in case["probes"]:
        expected = truth[p["id"]]
        # Aliases only describe the original value and cannot bless stale state.
        accepted = [expected] + (p["aliases"] if expected == p["expected"] else [])
        actual = answers.get(p["id"])
        missing = actual is None or normalize(actual).casefold() in {"", "unknown", "null"}
        correct = not missing and normalize(actual) in {normalize(v) for v in accepted}
        details.append({"id": p["id"], "kind": p["kind"], "expected": expected,
                        "actual": actual, "correct": correct, "missing": missing,
                        "wrong_nonempty": not correct and not missing,
                        "updated": p["id"] in changed})
    constraints = [d for d in details if d["kind"] == "constraint"]
    conflicts = [d for d in details if d["updated"]]
    return {"details": details, "correct": sum(d["correct"] for d in details),
            "total": len(details), "constraints_correct": sum(d["correct"] for d in constraints),
            "constraints_total": len(constraints),
            "wrong_nonempty": sum(d["wrong_nonempty"] for d in details),
            "missing": sum(d["missing"] for d in details),
            "updates_correct": sum(d["correct"] for d in conflicts),
            "updates_total": len(conflicts)}
