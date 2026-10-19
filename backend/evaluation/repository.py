"""Versioned evaluation rules and immutable reports, kept outside chat history."""

import json
import re
import uuid

from backend.conversation.store import atomic_json, internal_directory, now
from backend.modes.catalog import tags

from .catalog import builtin_bundle, upgrade_cases
from .models import DatasetInput


class EvaluationRepository:
    def __init__(self, root):
        self.root = root
        self.directory = internal_directory(root, "evaluation")
        self.dataset_path = self.directory / "dataset.json"
        bundle = builtin_bundle()
        if not self.dataset_path.exists():
            atomic_json(
                self.dataset_path,
                {
                    "name": bundle["name"],
                    "revision": 1,
                    "builtin_version": bundle["version"],
                    "cases": bundle["cases"],
                },
            )
        # Upgrade the earlier single-rule dataset once, preserving custom history rules.
        elif not self.dataset_path.is_symlink():
            previous = json.loads(self.dataset_path.read_text())
            if not previous.get("builtin_version"):
                ids = {case["id"] for case in previous["cases"]}
                previous["cases"] += [case for case in bundle["cases"] if case["id"] not in ids]
            if previous.get("builtin_version", 0) < bundle["version"]:
                previous["cases"] = upgrade_cases(previous["cases"], bundle)
                previous["builtin_version"] = bundle["version"]
                previous["revision"] += 1
                atomic_json(self.dataset_path, previous)

    def dataset(self):
        if self.dataset_path.is_symlink():
            raise ValueError("评测集路径无效")
        return DatasetInput.model_validate(json.loads(self.dataset_path.read_text())).model_dump()

    def save(self, data):
        current = self.dataset()
        if data.revision != current["revision"]:
            raise ValueError("评测集已被修改，请刷新后再保存")
        record = data.model_dump()
        record["builtin_version"] = current["builtin_version"]
        record["revision"] += 1
        atomic_json(self.dataset_path, record)
        return record

    def import_session(self, item, revision):
        if item.busy():
            raise ValueError("会话还在执行，请结束后导入以固定来源")
        dataset = self.dataset()
        messages = [
            event
            for event in item.store.events
            if event["type"] == "message" and event.get("role") == "user"
        ]
        if not messages:
            raise ValueError("会话没有可导入的用户任务")
        if any(
            case["source"].get("session_id") == item.store.session_id
            and case["source"].get("covered_seq") == item.store.events[-1]["seq"]
            for case in dataset["cases"]
        ):
            raise ValueError("这个版本的会话已经导入")
        dataset["revision"] = revision
        dataset["cases"].append(
            {
                "id": "session-" + uuid.uuid4().hex,
                "name": item.store.meta.get("title", "导入会话")[:120],
                "mode": "history",
                "category": "history",
                "prompts": [message["content"] for message in messages[-10:]],
                "input_contains": [messages[-1]["content"][:2000]],
                "expected_status": "completed",
                "source": {
                    "type": "session",
                    "session_id": item.store.session_id,
                    "covered_seq": item.store.events[-1]["seq"],
                    "message_ids": [message.get("message_id") for message in messages[-10:]],
                    "agent_modes": sorted(
                        {m.get("agent_mode") or "legacy_unknown" for m in messages}
                    ),
                    "mode_profiles": [tags(m) for m in messages[-10:]],
                },
            }
        )
        return self.save(DatasetInput.model_validate(dataset))

    def save_report(self, report):
        report = {**report, "id": uuid.uuid4().hex, "created_at": now()}
        directory = internal_directory(self.root, "evaluation", "reports")
        atomic_json(directory / f"{report['id']}.json", report)
        return report

    def reports(self):
        directory = self.directory / "reports"
        if not directory.exists():
            return []
        if directory.is_symlink():
            raise ValueError("报告路径无效")
        records = []
        for path in directory.glob("*.json"):
            if path.is_symlink():
                continue
            report = json.loads(path.read_text())
            records.append(
                {
                    k: report[k]
                    for k in ("id", "created_at", "dataset_name", "dataset_revision", "metrics")
                }
            )
        return sorted(records, key=lambda r: r["created_at"], reverse=True)

    def report(self, identifier):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("报告 ID 无效")
        path = self.directory / "reports" / f"{identifier}.json"
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("报告路径无效")
        return json.loads(path.read_text())
