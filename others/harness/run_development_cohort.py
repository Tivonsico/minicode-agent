"""A fresh, separately recorded real-model run of the fixed development set."""

import json
import re
import sys
import time
from pathlib import Path

import httpx

from backend.observability.inputs import digest

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / ".minicode/swe-bench"


def main():
    stage = sys.argv[1]
    manifest = json.loads(
        (ROOT / "docs/harness/swe-bench-25-development-manifest.json").read_text()
    )
    path = DIRECTORY / ("development-" + stage + ".json")
    if path.exists():
        raise SystemExit("Development record exists; refusing implicit rerun")
    versions = json.loads((ROOT / "docs/harness/swe-bench-25-repair-versions.json").read_text())
    expected = (
        manifest["source_backend_code_digest"]
        if stage == "baseline-repeat"
        else versions["repaired_backend_code_digest"]
    )
    if stage != "baseline-repeat":
        actual = digest(
            {
                str(p.relative_to(ROOT / "backend")): digest(p.read_bytes().hex())
                for p in sorted((ROOT / "backend").rglob("*.py"))
            }
        )
        assert actual == expected, "Repair version changed before comparison"
    body = dict(
        manifest["options"], instance_ids=[task["instance_id"] for task in manifest["tasks"]]
    )
    with httpx.Client(base_url="http://127.0.0.1:8765", timeout=60) as client:
        client.headers["x-minicode-token"] = re.search(
            r'name="minicode-token" content="([^"]+)"', client.get("/").text
        )[1]
        response = client.post("/api/swe-bench/jobs", json=body)
        response.raise_for_status()
        job = response.json()
        assert job["environment"]["native_profiles_digest"] == manifest["native_profiles_digest"]
        assert job["environment"]["backend_code_digest"] == expected
        record = {
            "stage": stage,
            "job_id": job["id"],
            "status": "running",
            "backend_code_digest": job["environment"]["backend_code_digest"],
            "native_profiles_digest": job["environment"]["native_profiles_digest"],
        }
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        print(stage, job["id"], flush=True)
        seen = set()
        while job["status"] == "running":
            time.sleep(5)
            response = client.get("/api/swe-bench/jobs/" + job["id"])
            response.raise_for_status()
            job = response.json()
            for row in job["results"]:
                if row["instance_id"] not in seen:
                    seen.add(row["instance_id"])
                    print(row["instance_id"], row["status"], row.get("agent_status"), flush=True)
        record["status"] = job["status"]
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
