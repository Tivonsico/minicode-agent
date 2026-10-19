"""Restore frozen amd64 images before the official harness's native pull path."""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import docker

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / ".minicode/swe-bench"
TASKS = json.loads((ROOT / "docs/harness/swe-bench-25-manifest.json").read_text())["tasks"]
BASELINES = {
    row["instance_id"]: row["image"]
    for row in json.loads((ROOT / "docs/harness/swe-bench-25-baseline-images.json").read_text())
}
os.environ["DOCKER_HOST"] = "unix://" + str(DIRECTORY / "lima/swe/sock/docker.sock")
LOCK = threading.Lock()


def pull(task):
    started = time.monotonic()
    record = {"instance_id": task["instance_id"], "stage": "grading_image_restore"}
    baseline = BASELINES[task["instance_id"]]
    try:
        with closing(docker.from_env(timeout=1800)) as client:
            try:
                image = client.images.get(task["image"])
            except docker.errors.ImageNotFound:
                image = client.images.pull(baseline["repo_digests"][0], platform="linux/amd64")
                repository, tag = task["image"].rsplit(":", 1)
                image.tag(repository, tag)
            assert image.id == baseline["image_id"], "Frozen baseline image differs"
            record.update(status="ready", image_id=image.id, platform="linux/amd64")
    except Exception as exc:
        record.update(status="environment_error", error=str(exc))
    record["duration_ms"] = round((time.monotonic() - started) * 1000)
    with LOCK:
        with (DIRECTORY / "grading-image-restore.jsonl").open("a") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(record["instance_id"], record["status"], record["duration_ms"], flush=True)
    return record


def main():
    scheduled = set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = []
        while True:
            campaign = json.loads((DIRECTORY / "campaign-25.json").read_text())
            if campaign["status"] != "running":
                break
            completed = set()
            for identifier in campaign["job_ids"]:
                job = json.loads((DIRECTORY / "jobs" / identifier / "job.json").read_text())
                completed.update(row["instance_id"] for row in job["results"])
            current = next(
                (i for i, task in enumerate(TASKS) if task["instance_id"] not in completed),
                len(TASKS),
            )
            pending = [future for future in pending if not future.done()]
            for index in range(current, min(current + 3, len(TASKS))):
                if index not in scheduled and len(pending) < 2:
                    scheduled.add(index)
                    pending.append(pool.submit(pull, TASKS[index]))
            time.sleep(3)


if __name__ == "__main__":
    main()
