"""Persistent reconciliation changes the latest outcome, never the old evidence."""

from .snapshot import identity


def unresolved_operations(events):
    results = {}
    resolved = set()
    for event in events:
        if event["type"] == "tool_result":
            results[identity(event, "call_id")] = event
        elif event["type"] == "operation_reconciled":
            resolved.add(event.get("result_seq"))
    return [
        e for e in results.values() if e.get("status") == "unknown" and e["seq"] not in resolved
    ]
