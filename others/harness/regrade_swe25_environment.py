"""Recover acceptance of the original patch; never request a new model output."""

import hashlib
import json
import re
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / ".minicode/swe-bench"


def main():
    record_path = DIRECTORY / "regrade-original-environment.json"
    if record_path.exists():
        raise SystemExit("Recovery record exists; no implicit repeated grading")
    campaign = json.loads((DIRECTORY / "campaign-25.json").read_text())
    source_id = campaign["job_ids"][0]
    source = json.loads((DIRECTORY / "jobs" / source_id / "job.json").read_text())
    identifier = "psf__requests-3362"
    body = dict(source["options"], instance_ids=[identifier])
    with httpx.Client(base_url="http://127.0.0.1:8765", timeout=60) as client:
        client.headers["x-minicode-token"] = re.search(
            r'name="minicode-token" content="([^"]+)"', client.get("/").text
        )[1]
        response = client.post("/api/swe-bench/jobs/" + source_id + "/regrade", json=body)
        response.raise_for_status()
        job = response.json()
        assert job["execution_reused"]
        record = {
            "job_id": job["id"],
            "source_job_id": source_id,
            "instance_id": identifier,
            "phase": "grader_recovery",
            "status": "running",
            "new_model_requests": 0,
        }
        record_path.write_text(json.dumps(record, indent=2) + "\n")
        print("regrade", job["id"], flush=True)
        while job["status"] == "running":
            time.sleep(2)
            job = client.get("/api/swe-bench/jobs/" + job["id"]).json()
        record["status"] = job["status"]
        record["results"] = job["results"]
        row = job["results"][0]
        original = next(row for row in source["results"] if row["instance_id"] == identifier)
        old = DIRECTORY / "jobs" / source_id / str(original["artifact_index"]) / "model.patch"
        new = DIRECTORY / "jobs" / job["id"] / "0" / "model.patch"
        assert old.read_bytes() == new.read_bytes()
        assert row["new_model_requests"] == 0
        record["patch_sha256"] = hashlib.sha256(old.read_bytes()).hexdigest()
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        print(identifier, row["status"], "new model requests 0", flush=True)


if __name__ == "__main__":
    main()
