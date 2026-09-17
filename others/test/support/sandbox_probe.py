"""Manual Windows sandbox probe; results are written under others/test_record."""

import json
import subprocess
import sys
from pathlib import Path


def main(executable: Path, record_root: Path) -> int:
    probe = record_root / "sandbox-probe"
    probe.mkdir(parents=True, exist_ok=True)
    internal = probe / ".minicode"
    internal.mkdir(exist_ok=True)
    secret = internal / "readme.txt"
    secret.write_text("can-read", encoding="utf-8")
    outside = record_root / "sandbox-outside.txt"
    outside.write_text("outside-marker", encoding="utf-8")
    commands = {
        "read_internal": "cmd.exe /c type .minicode\\readme.txt",
        "write_ordinary": "cmd.exe /c echo changed > ordinary.txt",
        "write_internal": "cmd.exe /c echo changed > .minicode\\readme.txt",
        "read_outside": f'cmd.exe /c type "{outside}"',
        "write_outside": f'cmd.exe /c echo changed > "{outside}"',
    }
    results = {}
    for index, (name, command) in enumerate(commands.items()):
        policy = {
            "version": "0.8.0-alpha",
            "containment": "processcontainer",
            "containerId": f"minicode-probe-{index}",
            "process": {"commandLine": command, "cwd": str(probe), "timeout": 10000},
            "filesystem": {
                "readwritePaths": [str(probe)],
                "readonlyPaths": [str(internal)],
            },
            "network": {
                "egress": {"default": "deny"},
                "ingress": {"default": "deny", "hostLoopback": "deny"},
            },
            "fallback": {"allowDaclMutation": True},
        }
        config = probe / f"policy-{index}.json"
        config.write_text(json.dumps(policy), encoding="utf-8")
        result = subprocess.run(
            [str(executable), str(config)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        results[name] = {
            "exit_code": result.returncode,
            "stdout": result.stdout[:1000],
            "stderr": result.stderr[:1000],
        }
    results["files"] = {
        "ordinary": (probe / "ordinary.txt").read_text(encoding="utf-8")
        if (probe / "ordinary.txt").exists()
        else None,
        "internal": secret.read_text(encoding="utf-8"),
        "outside": outside.read_text(encoding="utf-8"),
    }
    report = record_root / "sandbox-probe-result.json"
    report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]), Path(sys.argv[2])))
