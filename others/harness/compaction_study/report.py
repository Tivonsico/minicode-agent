"""Offline aggregation including the independently recorded summary fact audit."""

import argparse
import json
from collections import Counter
from pathlib import Path

from others.harness.compaction_study.paper_run import recompute
from others.harness.compaction_study.run import rescore, sha, write_json


def main(root):
    pilot = root / "pilot-v1"
    rescore(pilot)
    recompute(root / "paper-pilot-v1")
    scores = json.loads((pilot / "scores.json").read_text())
    audit = json.loads((pilot / "summary-fact-audit.json").read_text())
    if sha(pilot / "data.json") != audit["data_sha256"]:
        raise ValueError("Fact audit truth changed")
    fact_counts = {}
    for row in audit["rows"]:
        if sha(pilot / row["raw_result"]) != row["raw_result_sha256"]:
            raise ValueError("Fact audit source changed")
        counts = Counter(c["verdict"] for c in row["checks"])
        key = (row["arm"], row["round"])
        fact_counts.setdefault(key, Counter()).update(counts)
    for row in scores["by_arm_round"]:
        counts = fact_counts.get((row["arm"], row["round"]))
        row["summary_fact_audit"] = dict(counts) if counts else None
        row["task_completion_rate"] = None
    totals = []
    for arm in scores["by_arm_round"]:
        name = arm["arm"]
        if any(r["arm"] == name for r in totals):
            continue
        rows = [r for r in scores["rows"] if r["arm"] == name and r["status"] == "completed"]
        totals.append({"arm": name, "completed_rounds": len(rows),
                       **{k: sum(r[k] for r in rows) for k in [
                           "compression_total_tokens", "probe_total_tokens",
                           "compression_api_elapsed_ms", "probe_api_elapsed_ms"]},
                       "task_completion_rate": None})
    write_json(root / "comparison.json", {
        "note": "Probe accuracy, summary factual consistency, and lexical paper recall are distinct metrics. "
                "Latency is sum of individual request durations, not concurrent wall-clock time.",
        "by_arm_round": scores["by_arm_round"], "all_round_totals": totals,
        "audit_sha256": sha(pilot / "summary-fact-audit.json")})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    main(Path(parser.parse_args().root))
