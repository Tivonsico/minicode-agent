"""Independent assertion grading; never execute candidate code in the server."""

import json
import shutil
import sys
import tempfile
from pathlib import Path

from backend.execution.codex import sanitized_environment
from backend.execution.policy import sandbox_state
from backend.execution.process import capture


def read_file(work, name):
    path = work / name
    if path.is_symlink() or not path.resolve().is_relative_to(work.resolve()):
        raise ValueError("验收文件越界或是链接")
    if path.stat().st_size > 100000:
        raise ValueError("验收文件超过100000字节")
    return path.read_text(encoding="utf-8")


def same(actual, expected):
    # JSON numbers share a value domain; boolean values remain distinct.
    if type(actual) in {int, float} and type(expected) in {int, float}:
        return actual == expected
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(same(a, b) for a, b in zip(actual, expected))
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(
            same(actual[k], expected[k]) for k in actual
        )
    return actual == expected


async def verify(case, work, sandbox, *, phases=None):
    if not sandbox.available():
        raise ValueError("独立验收需要通过验证的操作系统沙箱")
    checks = [check for check in case["assertions"] if phases is None or check["phase"] in phases]
    calls = [check for check in checks if check["kind"] == "call"]
    returned = []
    if calls:
        with tempfile.TemporaryDirectory(prefix="minicode-grader-") as directory:
            grader = Path(directory).resolve()
            shutil.copyfile(Path(__file__).with_name("worker.py"), grader / "worker.py")
            inputs = [
                {key: check[key] for key in ("path", "symbol", "args", "kwargs")} for check in calls
            ]
            (grader / "input.json").write_text(json.dumps(inputs), encoding="utf-8")
            state = sandbox_state(work, [work, grader], sandbox.application_root)
            for entry in state["permissionProfile"]["file_system"]["entries"]:
                if entry["access"] == "write":
                    entry["access"] = "read"
            argv = [
                sandbox.executable,
                "sandbox",
                "--sandbox-state-json",
                json.dumps(state),
                "--",
                str(Path(sys.executable).resolve()),
                "-I",
                "-S",
                "-B",
                str(grader / "worker.py"),
                str(work),
                str(grader / "input.json"),
            ]
            try:
                code, stdout, _ = await capture(argv, work, sanitized_environment(), 12)
                values = json.loads(stdout) if code == 0 else []
                if isinstance(values, list) and len(values) == len(calls):
                    returned = values
            except (TimeoutError, ValueError):
                # A hanging/crashing candidate is a failed check, not a passing exit code.
                returned = []
    results, index = [], 0
    for check in checks:
        actual, passed = None, False
        try:
            if check["kind"] == "call":
                actual = (
                    returned[index]
                    if index < len(returned)
                    else {"error": "candidate_crashed_or_timed_out"}
                )
                index += 1
                passed = (
                    actual.get("exception") == check["raises"]
                    if check["raises"]
                    else "value" in actual and same(actual["value"], check["expected"])
                )
            elif check["kind"] == "file_absent":
                passed = (
                    not (work / check["path"]).exists() and not (work / check["path"]).is_symlink()
                )
                actual = "absent" if passed else "present"
            else:
                actual = read_file(work, check["path"])
                passed = (
                    check["expected"] in actual
                    if check["kind"] == "file_contains"
                    else actual == check["expected"]
                )
        except (OSError, ValueError, UnicodeError, AttributeError, TypeError):
            actual = {"error": "invalid_candidate_result"}
        results.append(
            {
                "name": check["name"],
                "phase": check["phase"],
                "passed": passed,
                "actual": actual,
                "expected": check["raises"] or check["expected"],
            }
        )
    return results
