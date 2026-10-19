"""Bundled, versioned task packages; no model calls or downloads at startup."""

import json
from pathlib import Path

from .models import Case


def builtin_bundle():
    source = Path(__file__).with_name("data") / "tasks.json"
    bundle = json.loads(source.read_text())
    planning = json.loads(source.with_name("planning.json").read_text())
    bundle["version"] = 3
    bundle["cases"] += planning["cases"]
    bundle["cases"] = [Case.model_validate(case).model_dump() for case in bundle["cases"]]
    return bundle


def builtin_cases():
    return builtin_bundle()["cases"]


def upgrade_cases(cases, bundle):
    previous = json.loads((Path(__file__).with_name("data") / "task_updates.json").read_text())
    defaults = {case["id"]: case for case in bundle["cases"]}
    upgraded = []
    for case in cases:
        current = Case.model_validate(case).model_dump()
        default = defaults.get(current["id"])
        # Upgrade unchanged shipped tasks; retain every user-customized case.
        old = {**default, "prompts": previous.get(current["id"])} if default else None
        upgraded.append(default if current == old else current)
    ids = {case["id"] for case in upgraded}
    upgraded += [
        case for case in bundle["cases"] if case["goal"] == "planning" and case["id"] not in ids
    ]
    return upgraded
