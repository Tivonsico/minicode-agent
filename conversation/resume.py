"""Crash recovery records partial evidence; it never invokes a tool."""

from collections import defaultdict


def recover_session(store):
    starts = [e for e in store.events if e["type"] == "turn_start"]
    ended = {e.get("turn_id") for e in store.events if e["type"] == "turn_end"}
    complete = {e["message_id"] for e in store.events if e["type"] == "message"}
    partials = defaultdict(str)
    for event in store.events:
        if event["type"] == "assistant_delta" and event.get("kind") == "text":
            partials[(event["turn_id"], event["message_id"])] += event.get("content", "")
    for (turn_id, message_id), content in partials.items():
        if message_id not in complete:
            store.append(
                "message",
                turn_id=turn_id,
                message_id=message_id,
                role="assistant",
                content=content,
                status="partial",
                is_final=False,
            )
    for start in starts:
        if start["turn_id"] not in ended:
            store.append(
                "turn_end", turn_id=start["turn_id"], status="interrupted", reason="process_exit"
            )
    return store
