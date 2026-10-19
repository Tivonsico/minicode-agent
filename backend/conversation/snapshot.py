"""Compact runtime projections; the original journal remains the audit source."""

import hashlib
import json

VERSION = 1
REDUCER_VERSION = 2
PAIRS = (
    ("turn_start", "turn_end", "turn_id"),
    ("request_start", "request_end", "request_id"),
    ("approval_requested", "approval_closed", "approval_id"),
    ("tool_start", "tool_end", "call_id"),
    ("step_start", "step_end", "step_id"),
    ("run_accepted", "run_end", "run_id"),
    ("run_start", "run_end", "run_id"),
)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def identity(event, key):
    if key == "call_id":
        return event.get("turn_id"), event.get("request_id"), event.get(key)
    return event.get(key)


def recovery_seed(events):
    selected = {}
    active_turns = set()
    for start, end, key in PAIRS:
        closed = {identity(e, key) for e in events if e["type"] == end}
        for event in events:
            if event["type"] != start or identity(event, key) in closed:
                continue
            selected[event["seq"]] = event
            if key == "turn_id":
                active_turns.add(event.get(key))
            if key == "call_id":
                for candidate in events:
                    if candidate["type"] in ("tool_running", "tool_result") and identity(
                        candidate, key
                    ) == identity(event, key):
                        selected[candidate["seq"]] = candidate
    complete = {e.get("message_id") for e in events if e["type"] == "message"}
    discarded = {e.get("message_id") for e in events if e["type"] == "request_recovery"}
    partials = {}
    for event in events:
        if (
            event["type"] == "assistant_delta"
            and event.get("kind") == "text"
            and event.get("turn_id") in active_turns
            and event.get("message_id") not in complete | discarded
        ):
            key = (event.get("turn_id"), event.get("message_id"), event.get("request_id"))
            if key not in partials:
                partials[key] = {**event, "content": ""}
            partials[key]["content"] += event.get("content", "")
    selected.update((e["seq"], e) for e in partials.values())
    return sorted(selected.values(), key=lambda e: e["seq"])


def runtime_view(events):
    """Keep model history and current execution, not old stream/audit payloads."""
    turn = next(
        (e.get("turn_id") for e in reversed(events) if e["type"] in ("turn_start", "run_accepted")),
        None,
    )
    always = {
        "message",
        "tool_result",
        "operation_reconciled",
        "directory_authorized",
        "directory_revoked",
        "directory_selected",
        "output_saved",
    }
    latest = {}
    # Retain the original creation boundary even when its event type is compacted.
    result = {events[0]["seq"]: events[0]} if events else {}
    complete = {e.get("message_id") for e in events if e["type"] == "message"}
    for e in events:
        kind = e["type"]
        if kind in ("assistant_delta", "tool_delta"):
            continue
        if kind in ("context_compressed", "mode_selected", "checkpoint", "task_state"):
            latest[kind] = e
        elif kind == "usage":
            latest[(kind, e.get("request_id"))] = e
        elif (
            kind in always
            or (turn is not None and e.get("turn_id") == turn)
            or (kind == "run_accepted" and e.get("input", {}).get("message_id") not in complete)
        ):
            result[e["seq"]] = e
    result.update((e["seq"], e) for e in latest.values())
    # Pending lifecycles may belong to an earlier background request.
    result.update((e["seq"], e) for e in recovery_seed(events))
    return sorted(result.values(), key=lambda e: e["seq"])


def runtime_events(store):
    return store.view if hasattr(store, "view") else store.events
