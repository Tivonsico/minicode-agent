"""Resolve and record every transitive native dependency before evaluation."""

import json
import shutil
import subprocess
from pathlib import Path

from backend.evaluation.swe_bench.environment import Environment

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "backend/evaluation/swe_bench/self_test_profiles.json"


def main():
    data = json.loads(PROFILES.read_text())
    directory = ROOT / ".minicode/swe-bench/preflight-25/locks"
    directory.mkdir(exist_ok=True)
    env = Environment(ROOT).variables()
    for name, profile in data["profiles"].items():
        requirements = directory / (name + ".in")
        pins = directory / (name + ".txt")
        requirements.write_text(
            "\n".join(
                profile["dependencies"] + ["setuptools==75.1.0", "wheel==0.44.0", "pip==24.2"]
            )
            + "\n"
        )
        result = subprocess.run(
            [
                shutil.which("uv"),
                "pip",
                "compile",
                "--no-header",
                "--no-annotate",
                "--python-version",
                "3.9",
                "--output-file",
                str(pins),
                str(requirements),
            ],
            env=env,
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        (directory / (name + ".log")).write_text(result.stdout + result.stderr)
        result.check_returncode()
        profile["dependencies"] = [
            line for line in pins.read_text().splitlines() if line and not line.startswith("#")
        ]
        print(name, len(profile["dependencies"]), flush=True)
    PROFILES.write_text(json.dumps(data, indent=2) + "\n")


if __name__ == "__main__":
    main()
