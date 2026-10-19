"""Read-only timeline and metrics projected from journal facts."""

from collections import Counter
from datetime import datetime

from backend.modes.catalog import tags

from .inputs import read_input, redact


def elapsed(start, end):
    try:
        return max(
            0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000
        )
    except (TypeError, ValueError):
        return None


def percentile(values, fraction):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    index = (len(values) - 1) * fraction
    lower = int(index)
    return round(
        values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (index - lower),
        3,
    )


def project(events):
    turns = {}
    requests = {}
    results = []
    for event in events:
        kind = event["type"]
        if kind == "turn_start":
            turns[event["turn_id"]] = {
                **tags(event),
                "turn_id": event["turn_id"],
                "run_id": event.get("run_id"),
                "started_at": event["time"],
                "status": "running",
            }
        elif kind == "turn_end" and event.get("turn_id") in turns:
            turn = turns[event["turn_id"]]
            turn.update(
                status=event["status"],
                reason=event.get("reason"),
                ended_at=event["time"],
                duration_ms=elapsed(turn["started_at"], event["time"]),
            )
        elif kind == "message" and event.get("role") == "user" and event.get("turn_id") in turns:
            turns[event["turn_id"]]["objective"] = event["content"]
        elif kind == "request_start":
            requests[event["request_id"]] = dict(event)
        elif kind == "request_progress" and event.get("request_id") in requests:
            request = requests[event["request_id"]]
            request.setdefault("first_output_ms", event.get("elapsed_ms"))
            if event.get("kind") == "text":
                request.setdefault("first_text_ms", event.get("elapsed_ms"))
        elif kind == "request_end" and event.get("request_id") in requests:
            requests[event["request_id"]].update(
                {
                    k: event.get(k)
                    for k in ("status", "duration_ms", "error_type", "retrying", "finish_reason")
                }
            )
        elif kind == "tool_result":
            results.append(event)
    # Legacy logs retain their original identities and missing values.
    for request in requests.values():
        if "first_output_ms" not in request:
            deltas = [
                e
                for e in events
                if e.get("request_id") == request["request_id"]
                and e["type"] in {"assistant_delta", "tool_delta"}
            ]
            if deltas:
                request["first_output_ms"] = elapsed(request["time"], deltas[0]["time"])
            text = next((e for e in deltas if e.get("kind") == "text"), None)
            if text:
                request["first_text_ms"] = elapsed(request["time"], text["time"])
    usage = {e["request_id"]: e for e in events if e["type"] == "usage" and e.get("request_id")}
    unknown = sum(
        not isinstance(e.get("usage"), dict) or e["usage"].get("total_tokens") is None
        for e in usage.values()
    )
    tokens = sum((e.get("usage") or {}).get("total_tokens") or 0 for e in usage.values())
    failures = Counter(
        e.get("error_type") or e.get("reason") or "unknown"
        for e in events
        if (e["type"] == "request_end" and e.get("status") == "failed")
        or (e["type"] == "tool_result" and e.get("status") in {"failed", "unknown"})
    )
    metrics = {
        "turns": len(turns),
        "completed_turns": sum(t["status"] == "completed" for t in turns.values()),
        "requests": len(requests),
        "retries": sum(e.get("attempt", 1) > 1 for e in requests.values()),
        "known_tokens": tokens,
        "unknown_usage_requests": unknown,
        "usage_coverage": (len(usage) - unknown) / len(usage) if usage else None,
        "tool_calls": len(results),
        "tool_failures": sum(e.get("status") in {"failed", "unknown"} for e in results),
        "turn_duration_p50_ms": percentile([t.get("duration_ms") for t in turns.values()], 0.5),
        "turn_duration_p95_ms": percentile([t.get("duration_ms") for t in turns.values()], 0.95),
        "first_text_p50_ms": percentile([r.get("first_text_ms") for r in requests.values()], 0.5),
        "first_text_p95_ms": percentile([r.get("first_text_ms") for r in requests.values()], 0.95),
        "approval_wait_ms": sum(
            (e.get("duration_ms") or 0) for e in events if e["type"] == "approval_closed"
        ),
        "failure_types": dict(failures),
        "cost": None,
    }
    return {"turns": list(turns.values()), "requests": list(requests.values()), "metrics": metrics}


def export(store):
    data = project(store.events)
    refs = {e["input_ref"] for e in store.events if e.get("input_ref")}
    inputs = {}
    for ref in refs:
        try:
            inputs[ref] = read_input(store, ref)
        except (OSError, ValueError):
            inputs[ref] = {"unavailable": True}
    return redact(
        {
            "schema_version": 1,
            "session_id": store.session_id,
            "events": store.events,
            "inputs": inputs,
            **data,
        },
        store.secrets,
    )
