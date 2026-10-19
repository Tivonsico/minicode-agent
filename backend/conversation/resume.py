"""Crash recovery records partial evidence; it never invokes a tool."""

from collections import defaultdict

from backend.conversation.snapshot import identity as event_identity
from backend.modes.catalog import IDENTITY_FIELDS
from backend.modes.catalog import tags as mode_tags


def recover_session(store):
    starts = [e for e in store.recovery_view if e["type"] == "turn_start"]
    ended = {e.get("turn_id") for e in store.recovery_view if e["type"] == "turn_end"}
    complete = {e["message_id"] for e in store.recovery_view if e["type"] == "message"}
    discarded = {
        e.get("message_id") for e in store.recovery_view if e["type"] == "request_recovery"
    }
    partials = defaultdict(str)
    origins = {e["turn_id"]: e for e in starts}
    for event in store.recovery_view:
        if (
            event["type"] == "assistant_delta"
            and event.get("kind") == "text"
            and event.get("message_id") not in discarded
            and event.get("turn_id") not in ended
        ):
            partials[(event["turn_id"], event["message_id"])] += event.get("content", "")
    for (turn_id, message_id), content in partials.items():
        if message_id not in complete:
            store.append(
                "message",
                turn_id=turn_id,
                run_id=origins.get(turn_id, {}).get("run_id"),
                message_id=message_id,
                role="assistant",
                content=content,
                status="partial",
                is_final=False,
                **mode_tags(origins.get(turn_id, {})),
            )
    for start in starts:
        if start["turn_id"] not in ended:
            store.append(
                "turn_end",
                turn_id=start["turn_id"],
                run_id=start.get("run_id"),
                status="interrupted",
                reason="process_exit",
                **mode_tags(start),
            )
    # Close recorded lifecycles once. Recovery displays evidence, never runs tools.
    pairs = (
        ("request_start", "request_end", "request_id"),
        ("approval_requested", "approval_closed", "approval_id"),
        ("tool_start", "tool_end", "call_id"),
        ("step_start", "step_end", "step_id"),
        ("run_accepted", "run_end", "run_id"),
        ("run_start", "run_end", "run_id"),
    )
    for start_type, end_type, key in pairs:
        closed = {event_identity(e, key) for e in store.recovery_view if e["type"] == end_type}
        for event in list(store.recovery_view):
            identity = event_identity(event, key)
            if event["type"] != start_type or not identity or identity in closed:
                continue
            tags = {
                k: event[k]
                for k in (
                    "run_id",
                    "turn_id",
                    "step_id",
                    "request_id",
                    "call_id",
                    "approval_id",
                    *IDENTITY_FIELDS,
                )
                if k in event
            }
            legacy_completed = event.get("schema_version") is None and event.get("turn_id") in ended
            store.append(
                end_type,
                **tags,
                status="unknown" if legacy_completed else "interrupted",
                reason="legacy_missing_end" if legacy_completed else "process_exit",
                duration_ms=None,
            )
            if start_type == "tool_start" and not any(
                e["type"] == "tool_result" and event_identity(e, key) == identity
                for e in store.recovery_view
            ):
                running = any(
                    e["type"] == "tool_running" and event_identity(e, key) == identity
                    for e in store.recovery_view
                )
                store.append(
                    "tool_result",
                    **tags,
                    name=event.get("name"),
                    status="unknown" if running else "cancelled",
                    content="进程退出；操作结果需核对，未自动重跑。",
                    error_type="process_exit",
                )
            closed.add(identity)
    return store
