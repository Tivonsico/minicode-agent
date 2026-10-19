"""Run the predeclared 10+10+5 cohort once through the real application API."""

import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from backend.evaluation.swe_bench.environment import Environment
from backend.evaluation.swe_bench.self_test import profile
from backend.observability.inputs import digest

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / ".minicode/swe-bench"
MANIFEST = ROOT / "docs/harness/swe-bench-25-manifest.json"
RECORD = DIRECTORY / "campaign-25.json"


def backend_digest():
    return digest(
        {
            str(path.relative_to(ROOT / "backend")): digest(path.read_bytes().hex())
            for path in sorted((ROOT / "backend").rglob("*.py"))
        }
    )


def save(record):
    temporary = RECORD.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(RECORD)


def remove_image(task):
    env = Environment(ROOT)
    code = (
        "import docker,sys; from contextlib import closing; "
        "client=docker.from_env(timeout=15); "
        "client.images.remove(sys.argv[1]); client.close()"
    )
    result = subprocess.run(
        [str(env.python), "-c", code, task["image"]],
        env=env.variables(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {
        "instance_id": task["instance_id"],
        "removed": result.returncode == 0,
        "error": result.stderr[-1500:] if result.returncode else None,
    }


def main():
    manifest = json.loads(MANIFEST.read_text())
    profiles = json.loads(
        (ROOT / "backend/evaluation/swe_bench/self_test_profiles.json").read_text()
    )
    for task in manifest["tasks"]:
        preflight = json.loads(
            (DIRECTORY / "preflight-25" / str(task["index"]) / "result.json").read_text()
        )
        assert preflight["status"] == "ready" and preflight["baseline_patch_empty"]
        _, configuration = profile(task)
        assert preflight["self_test_environment"]["profile_digest"] == digest(configuration)
    if RECORD.exists():
        raise SystemExit("Campaign already exists; refusing implicit repeat model calls")
    record = {
        "phase": "first",
        "status": "running",
        "planned": 25,
        "job_ids": [],
        "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "backend_code_digest": backend_digest(),
        "native_profiles_digest": digest(profiles),
        "image_cleanup": [],
    }
    snapshot = DIRECTORY / "campaign-25-source"
    for path in (ROOT / "backend").rglob("*"):
        if path.is_file() and path.suffix in {".py", ".json"}:
            destination = snapshot / path.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    save(record)
    with httpx.Client(base_url="http://127.0.0.1:8765", timeout=60) as client:
        page = client.get("/").text
        client.headers["x-minicode-token"] = re.search(
            r'name="minicode-token" content="([^"]+)"', page
        )[1]
        info = client.get("/api/info").json()
        assert info["model"] == manifest["model"] and info["sandbox_available"]
        assert not any(j["status"] == "running" for j in client.get("/api/swe-bench/jobs").json())
        for batch in range(1, 4):
            assert backend_digest() == record["backend_code_digest"]
            tasks = [task for task in manifest["tasks"] if task["batch"] == batch]
            body = dict(manifest["options"], instance_ids=[task["instance_id"] for task in tasks])
            response = client.post("/api/swe-bench/jobs", json=body)
            response.raise_for_status()
            job = response.json()
            record["job_ids"].append(job["id"])
            save(record)
            assert job["environment"]["backend_code_digest"] == record["backend_code_digest"]
            assert job["environment"]["native_profiles_digest"] == record["native_profiles_digest"]
            print("batch", batch, "job", job["id"], flush=True)
            completed = set()
            while True:
                response = client.get("/api/swe-bench/jobs/" + job["id"])
                response.raise_for_status()
                job = response.json()
                for row in job["results"]:
                    if row["instance_id"] in completed:
                        continue
                    completed.add(row["instance_id"])
                    print(row["instance_id"], row["status"], row.get("agent_status"), flush=True)
                    if row.get("container_cleanup") == "confirmed":
                        task = next(t for t in tasks if t["instance_id"] == row["instance_id"])
                        record["image_cleanup"].append(remove_image(task))
                        save(record)
                if job["status"] != "running":
                    break
                time.sleep(5)
            if job["status"] != "completed":
                record.update(status=job["status"], stopped_job=job["id"])
                save(record)
                raise SystemExit("Application job did not finish; inspect without implicit replay")
        record["status"] = "completed"
        save(record)


if __name__ == "__main__":
    main()
