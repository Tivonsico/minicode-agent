"""Offline feasibility ablation over the public-sample paper pilot inputs."""

import argparse
import time
from pathlib import Path

from others.harness.compaction_study.guarded import UnsafeCompaction, compact_preserving_spans
from others.harness.compaction_study.paper_run import upstream_modules
from others.harness.compaction_study.run import sha, verify_evidence, write_json


def main(output):
    verify_evidence(output)
    classifier, _, _, preservation, helpers, _ = upstream_modules(output / "upstream")
    rows = []
    for path in sorted((output / "inputs").glob("sample-*.md")):
        kb = helpers["build_kb_e2"](path)
        required = [i.text for i in kb.by_type(classifier.KnowledgeType.CONSTRAINT)]
        current = helpers["_kb_text"](kb)
        budget = max(64, int(kb.total_tokens * 0.5))
        for r in range(1, 6):
            started = time.perf_counter()
            try:
                candidate = compact_preserving_spans(current, required, max_chars=budget * 4)
                accepted, reason = True, None
            except UnsafeCompaction as exc:
                candidate, accepted, reason = current, False, str(exc)
            rows.append({"config": path.name, "round": r, "accepted": accepted,
                         "budget_chars": budget * 4, "before": current, "after": candidate,
                         "reason": reason, "actual_chars": len(candidate),
                         "budget_met": len(candidate) <= budget * 4,
                         "c_recall": sum(preservation.constraint_preserved(c, candidate)
                                         for c in required) / len(required),
                         "api_tokens": 0, "api_calls": 0,
                         "elapsed_ms": (time.perf_counter() - started) * 1000})
            current = candidate
            # A rejected attempt cannot claim to have freed memory. Same request is
            # retried to expose non-progress, with no silent enlargement or loss.
            if accepted:
                budget = max(64, int(len(current) // 4 * 0.5))
    write_json(output / "guarded-scores.json", {
        "candidate": "verbatim constraint spans + explicit infeasible-budget refusal",
        "source_sha256": sha(Path(__file__).with_name("guarded.py")),
        "required_source": "upstream regex classifier, not manually supplied oracle labels",
        "note": "Rejected outputs retain input and exceed requested budget; not successful compression. "
                "Static rules only; no dynamic task-state or task-completion claim.",
        "rows": rows})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-output", required=True)
    main(Path(parser.parse_args().paper_output))
