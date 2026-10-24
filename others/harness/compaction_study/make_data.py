"""Generate explicitly synthetic replacement data without using a model."""

import json
from pathlib import Path


def dataset():
    cases = []
    for name, region, port in [("payments", "ap-south-2", "8427"),
                               ("migration", "eu-west-3", "9371"),
                               ("compiler", "us-east-2", "7159")]:
        constraints = [
            ("protected", "Which exact directory must never be modified?", f"/{name}/archive", None),
            ("network", "Is outbound network access allowed or forbidden?", "forbidden", None),
            ("dependency", "Are new third-party dependencies allowed or forbidden?", "forbidden", None),
            ("region", "What is the sole permitted deployment region?", region, None),
            ("port", "What exact port must be used?", port, None),
            ("retry", "What is the maximum retry count?", "2", ["two"]),
            ("log", "What exact log field must be redacted?", f"{name}_secret", None),
            ("approval", "What approval mode is required?", "manual", None),
            ("cache", "Is local caching allowed or forbidden?", "allowed", None),
            ("encoding", "What encoding is required?", "UTF-8", ["utf8"]),
            ("schema", "What is the required schema version?", "v7", ["7"]),
            ("tests", "What exact command must be used to validate?", f"pytest tests/test_{name}.py", None),
        ]
        facts = [
            ("target", "What is the current target file path?", f"src/{name}/v1.py", None),
            ("status", "What is the current verification status?", "not_run", ["not run"]),
            ("owner", "Who is the current reviewer?", "Lin", None),
            ("branch", "What is the working branch?", f"study/{name}", None),
            ("artifact", "What is the exact output artifact path?", f"reports/{name}.json", None),
            ("error", "What exact error code was last observed?", "E317", None),
        ]
        probes = [dict(id=k, kind=kind, question=q, expected=v, aliases=a or [])
                  for kind, rows in [("constraint", constraints), ("fact", facts)]
                  for k, q, v, a in rows]
        initial = f"Implement the {name} repair. These user requirements remain binding.\n" + "\n".join([
            f"Never modify /{name}/archive.", "Outbound network access is forbidden.",
            "Adding third-party dependencies is forbidden.", f"Deploy only in {region}.",
            f"Use port {port}.", "Maximum retry count is 2.", f"Redact {name}_secret from logs.",
            "Approval mode must be manual.", "Local caching is allowed.", "Encoding must be UTF-8.",
            "Required schema version is v7.", f"Validate with: pytest tests/test_{name}.py",
            f"Current target file: src/{name}/v1.py", "Verification status: not_run",
            "Reviewer: Lin", f"Working branch: study/{name}",
            f"Output artifact: reports/{name}.json", "Last observed error code: E317",
        ])
        rounds = []
        for r in range(1, 6):
            updates = {}
            text = ""
            if r == 2:
                updates = {"target": f"src/{name}/v2.py", "owner": "Chen"}
                text = (f"User correction: target is now src/{name}/v2.py, replacing v1.py. "
                        "Reviewer is now Chen, replacing Lin. Other requirements remain unchanged.\n")
            if r == 4:
                updates = {"status": "failed", "error": "E928"}
                text = ("Actual test observation: verification status is failed, replacing not_run; "
                        "last observed error code is E928, replacing E317. This is not success.\n")
            noise = "\n".join(
                f"Read-only inspection round {r}, item {i}: src/{name}/helper_{i:02}.py "
                "contains parsing and formatting helpers. No source edits, commands or tests "
                "were executed by this inspection. No new requirements were issued."
                for i in range(18)
            )
            rounds.append({"round": r, "messages": [{"role": "user", "content": text + noise}],
                           "updates": updates})
        cases.append({"id": name, "initial": [{"role": "user", "content": initial}],
                      "probes": probes, "rounds": rounds})
    return {"schema_version": 1, "provenance": "synthetic replacement; NOT paper data",
            "cases": cases}


if __name__ == "__main__":
    Path(__file__).with_name("data.json").write_text(
        json.dumps(dataset(), ensure_ascii=False, indent=2) + "\n")
