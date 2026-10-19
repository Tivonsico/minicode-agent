"""Keep the small Lima disk clear while preparing the fixed campaign."""

import json
import os
import time
from contextlib import closing
from pathlib import Path

import docker

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / "docs/harness/swe-bench-25-manifest.json").read_text())
PREPARED = ROOT / ".minicode/swe-bench/preflight-25"
os.environ["DOCKER_HOST"] = "unix://" + str(ROOT / ".minicode/swe-bench/lima/swe/sock/docker.sock")


def main():
    with closing(docker.from_env(timeout=15)) as client:
        recorded = set()
        while len(recorded) < len(MANIFEST["tasks"]):
            for task in MANIFEST["tasks"]:
                identifier = task["instance_id"]
                if identifier in recorded:
                    continue
                path = PREPARED / str(task["index"]) / "result.json"
                if not path.is_file():
                    continue
                result = json.loads(path.read_text())
                if result["status"] == "ready":
                    try:
                        client.images.remove(task["image"])
                        print("removed", identifier, flush=True)
                    except docker.errors.ImageNotFound:
                        pass
                    except docker.errors.APIError as exc:
                        print("retry", identifier, str(exc), flush=True)
                        continue
                recorded.add(identifier)
            time.sleep(5)


if __name__ == "__main__":
    main()
