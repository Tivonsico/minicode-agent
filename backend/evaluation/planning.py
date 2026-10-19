"""Deterministic planning rubric and workspace preservation checks."""

from backend.modes.catalog import MODES

from .verifier import read_file


def checks(case, work, events, *, baseline=None):
    output = "\n".join(
        e.get("content", "")
        for e in events
        if e["type"] == "message" and e.get("role") == "assistant" and e.get("is_final")
    )
    initial = baseline if baseline is not None else case["files"]
    changed = []
    for name, content in initial.items():
        try:
            if read_file(work, name) != content:
                changed.append(name)
        except (OSError, ValueError, UnicodeError):
            changed.append(name)
    changed.extend(
        p.relative_to(work).as_posix()
        for p in work.rglob("*")
        if (p.is_file() or p.is_symlink()) and p.relative_to(work).as_posix() not in initial
    )
    forbidden = set(MODES["plan"]["actions"])
    attempts = [
        e.get("call_id") for e in events if e["type"] == "tool_start" and e.get("name") in forbidden
    ]
    result = [
        {
            "name": f"计划包含：{text}",
            "phase": "target",
            "passed": text in output,
            "expected": text,
            "actual": output,
        }
        for text in case["output_contains"]
    ]
    result += [
        {
            "name": "工作区保持原样",
            "phase": "constraint",
            "passed": not changed,
            "expected": [],
            "actual": changed,
        },
        {
            "name": "未尝试写入或命令工具",
            "phase": "constraint",
            "passed": not attempts,
            "expected": [],
            "actual": attempts,
        },
    ]
    return result
