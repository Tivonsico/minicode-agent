"""Select from public metadata only; never open reference patches or tests."""

import hashlib
import json
import subprocess
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = ROOT / ".minicode/swe-bench/task-repo"
REVISION = "3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d"
SEED = "minicode-unseen-25-2026-10-12-v1"
REPOS = sorted(
    [
        "django/django",
        "pytest-dev/pytest",
        "psf/requests",
        "sphinx-doc/sphinx",
        "sympy/sympy",
        "pylint-dev/pylint",
    ]
)


def git(*args):
    return subprocess.check_output(["git", "-C", str(REPOSITORY), *args]).decode().strip()


def main():
    assert git("rev-parse", "HEAD") == REVISION
    excluded = set()
    histories = []
    for path in sorted((ROOT / ".minicode/swe-bench/jobs").glob("*/job.json")):
        job = json.loads(path.read_text())
        histories.append(job["id"])
        for row in job.get("public_tasks", []) + job.get("results", []):
            excluded.add(row["instance_id"])
        if job.get("current"):
            excluded.add(job["current"]["instance_id"])
    eligible = defaultdict(list)
    for path in sorted((REPOSITORY / "tasks").glob("*/task.yaml")):
        row = yaml.safe_load(path.read_text())
        if (
            row["repo"] not in REPOS
            or row["split"] != "test"
            or "SWE-bench/SWE-bench_Lite" not in row.get("datasets", [])
            or row["instance_id"] in excluded
        ):
            continue
        public = {k: row[k] for k in ("instance_id", "repo", "base_commit", "version", "image")}
        public["metadata_blob"] = git("rev-parse", f"HEAD:tasks/{row['instance_id']}/task.yaml")
        assert git("hash-object", str(path)) == public["metadata_blob"]
        public["selection_rank"] = hashlib.sha256(
            (SEED + ":" + row["instance_id"]).encode()
        ).hexdigest()
        eligible[row["repo"]].append(public)
    for rows in eligible.values():
        rows.sort(key=lambda row: row["selection_rank"])
    selected = []
    for position in range(5):
        for repo in REPOS:
            if len(selected) < 25 and position < len(eligible[repo]):
                selected.append(eligible[repo][position])
    assert len(selected) == 25 and len({r["instance_id"] for r in selected}) == 25
    manifest = {
        "schema_version": 1,
        "task_revision": REVISION,
        "dataset_id": "swe_lite",
        "source": "https://github.com/SWE-bench/swe-bench-tasks.git",
        "split": "test",
        "selection_seed": SEED,
        "eligible_repositories": REPOS,
        "selection_method": "SHA256(seed + ':' + instance_id), ascending per repository; sorted repository round-robin, first 25",
        "repository_scope_reason": "Native macOS self-test scope: six Python repositories without mandatory compiled scientific extensions; not all Lite repositories",
        "excluded_instance_ids": sorted(excluded),
        "history_job_ids": histories,
        "eligible_counts": {repo: len(eligible[repo]) for repo in REPOS},
        "options": {
            "dataset_id": "swe_lite",
            "agent_mode": "auto",
            "approval_behavior": "approve",
            "reasoning_effort": "low",
            "agent_timeout_seconds": 600,
            "grader_timeout_seconds": 900,
        },
        "model": "GLM-5.3-Flash",
        "request_limit": 30,
        "harness_version": "5.0.2",
        "tasks": [dict(row, index=i, batch=i // 10 + 1) for i, row in enumerate(selected)],
    }
    destination = ROOT / "docs/harness/swe-bench-25-manifest.json"
    if destination.exists():
        raise SystemExit("Manifest already fixed; refusing to overwrite")
    destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
