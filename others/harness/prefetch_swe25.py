"""Bounded lookahead for official images; no model or candidate-code changes."""

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import docker

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / ".minicode/swe-bench"
TASKS = json.loads((ROOT / "docs/harness/swe-bench-25-manifest.json").read_text())["tasks"]
os.environ["DOCKER_HOST"] = "unix://" + str(DIRECTORY / "lima/swe/sock/docker.sock")


def pull(task):
    with closing(docker.from_env(timeout=1800)) as client:
        try:
            client.images.get(task["image"])
        except docker.errors.ImageNotFound:
            client.images.pull(task["image"], platform="linux/amd64")
    print("prefetched", task["instance_id"], flush=True)


def main():
    scheduled = set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = []
        while True:
            started = [
                int(p.name) for p in (DIRECTORY / "preflight-25").iterdir() if p.name.isdigit()
            ]
            current = max(started, default=-1)
            if current == len(TASKS) - 1:
                break
            pending = [future for future in pending if not future.done()]
            environment = dict(os.environ, LIMA_HOME=str(DIRECTORY / "lima"))
            free = (
                subprocess.check_output(
                    [
                        str(DIRECTORY / "runtime/bin/limactl"),
                        "shell",
                        "swe",
                        "df",
                        "--output=avail",
                        "-B1",
                        "/",
                    ],
                    env=environment,
                )
                .decode()
                .splitlines()[-1]
            )
            if int(free) > 12_000_000_000:
                for index in range(current + 1, min(current + 3, len(TASKS))):
                    if index not in scheduled and len(pending) < 2:
                        scheduled.add(index)
                        pending.append(pool.submit(pull, TASKS[index]))
            time.sleep(5)


if __name__ == "__main__":
    main()
