"""Offline request census; byte attribution is explicitly NOT token attribution."""

import collections
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def analyze(paths):
    counts = collections.Counter()
    usage = collections.Counter()
    purposes = collections.Counter()
    sizes = collections.Counter()
    tool_sizes = collections.Counter()
    assistant_fields = collections.Counter()
    models = collections.Counter()
    evidence = []
    seen = set()
    for path in paths:
        raw = path.read_bytes()
        events = ([json.loads(line) for line in raw.splitlines() if line.strip()]
                  if path.suffix == ".jsonl" else json.loads(raw)["events"])
        counts["files"] += 1
        event_types = collections.Counter(e["type"] for e in events)
        for kind in ("context_compressed", "context_compression_error", "context_overflow"):
            counts[kind] += event_types[kind]
        last_usage = {e["request_id"]: e.get("usage") for e in events
                      if e["type"] == "usage" and e.get("request_id")}
        names = {}
        for event in events:
            if event["type"] == "tool_start":
                names[event.get("call_id")] = event.get("name", "unknown")
        count_before = counts["requests"]
        for event in events:
            if event["type"] != "request_start":
                continue
            identity = (event.get("session_id"), event["request_id"])
            if identity in seen:
                counts["duplicate_request_exports_skipped"] += 1
                continue
            seen.add(identity)
            counts["requests"] += 1
            purpose = event.get("purpose", "main")
            purposes[purpose] += 1
            record = last_usage.get(event["request_id"])
            if not record or record.get("total_tokens") is None:
                counts["unknown_usage_requests"] += 1
            else:
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if isinstance(record.get(key), (int, float)):
                        usage[key] += record[key]
                for section, key in (("prompt_tokens_details", "cached_tokens"),
                                     ("completion_tokens_details", "reasoning_tokens")):
                    amount = (record.get(section) or {}).get(key)
                    if isinstance(amount, (int, float)):
                        usage[key] += amount
                        counts[key + "_reported_requests"] += 1
            if not event.get("input_ref"):
                counts["missing_input_refs"] += 1
                continue
            candidate = path.parent / event["input_ref"]
            if not candidate.exists():
                candidate = path.parent / "runtime/sessions" / event["session_id"] / event["input_ref"]
            if not candidate.exists():
                counts["missing_input_files"] += 1
                continue
            payload = json.loads(candidate.read_text())["payload"]
            models[payload.get("model", "unknown")] += 1
            counts["inputs_read"] += 1
            if purpose != "main":
                continue
            counts["main_inputs_read"] += 1
            sizes["tool_definitions"] += encoded_size(payload.get("tools", []))
            for message in payload["messages"]:
                role = message["role"]
                bucket = "system_developer" if role in {"system", "developer"} else role
                sizes[bucket] += encoded_size(message)
                if role == "assistant":
                    for field in ("content", "tool_calls", "reasoning_content"):
                        if field in message:
                            assistant_fields[field] += encoded_size(message[field])
                if role == "tool":
                    tool_sizes[names.get(message.get("tool_call_id"), "unknown")] += encoded_size(message)
        evidence.append({"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(raw).hexdigest(),
                         "requests": counts["requests"] - count_before})
    total = sum(sizes.values())
    return {"counts": dict(counts), "purposes": dict(purposes), "known_usage": dict(usage),
            "models": dict(models), "main_request_component_json_bytes": dict(sizes),
            "main_request_component_byte_shares": {k: round(v / total, 6) for k, v in sizes.items()},
            "tool_result_json_bytes_by_tool": dict(tool_sizes),
            "assistant_field_value_json_bytes": dict(assistant_fields), "evidence": evidence}


if __name__ == "__main__":
    data = ROOT / ".minicode"
    all_paths = sorted((data / "sessions").glob("*/session.jsonl")) + sorted(
        (data / "swe-bench/jobs").glob("**/trace.json"))
    frozen = json.loads((ROOT / "docs/harness/swe-bench-25-first-results.json").read_text())
    first_paths = [data / "swe-bench/jobs" / r["job_id"] / str(r["artifact_index"]) / "trace.json"
                   for r in frozen["results"]]
    report = {
        "date": "2026-10-21",
        "method": "Latest usage per request_id, request_start census; duplicate request exports skipped by "
                  "(session_id, request_id); repeated messages counted per transmission. "
                  "Component JSON UTF-8 bytes exclude outer payload framing; not tokenizer counts or billing. "
                  "Cache/reasoning fields are subsets, never added again to total_tokens. "
                  "All-discovered cohort includes development runs; frozen-first-25 is a separate subset.",
        "all_discovered": analyze(all_paths), "frozen_first_25": analyze(first_paths),
    }
    destination = ROOT / "docs/research/minicode-context-costs-20261021.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for name in ("all_discovered", "frozen_first_25"):
        print(name, json.dumps({k: v for k, v in report[name].items() if k != "evidence"}, ensure_ascii=False))
