"""Rebuildable session metadata derived from journal events."""

from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_meta(view, session_id) -> dict:
    messages = {}
    for e in view:
        if e["type"] == "run_accepted" and isinstance(e.get("input"), dict):
            messages[e["input"]["message_id"]] = {**e, **e["input"]}
        elif e["type"] == "message" and "message_id" in e:
            messages[e["message_id"]] = e
    usage = {e["request_id"]: e for e in view if e["type"] == "usage" and "request_id" in e}
    by_purpose: dict[str, dict] = {}
    total = 0
    unknown = 0
    for event in usage.values():
        bucket = by_purpose.setdefault(event.get("purpose", "main"), {"tokens": 0, "unknown": 0})
        record = event.get("usage")
        if record is None or record.get("total_tokens") is None:
            unknown += 1
            bucket["unknown"] += 1
        else:
            amount = record["total_tokens"]
            total += amount
            bucket["tokens"] += amount
    users = [e for e in messages.values() if e.get("role") == "user"]
    endings = [e for e in view if e["type"] == "turn_end"]
    starts = [e for e in view if e["type"] == "turn_start"]
    active = bool(starts and (not endings or starts[-1]["seq"] > endings[-1]["seq"]))
    final_messages = [e for e in messages.values() if e.get("is_final")]
    return {
        "session_id": session_id,
        "title": users[0].get("content", "")[:60] if users else "新会话",
        "summary": final_messages[-1].get("content", "")[:200] if final_messages else "",
        "message_count": len(messages),
        "token_total": total,
        "usage_incomplete": bool(unknown),
        "unknown_requests": unknown,
        "usage_by_purpose": by_purpose,
        "created_at": view[0]["time"] if view else now(),
        "last_active_at": view[-1]["time"] if view else now(),
        "status": "active" if active else "idle",
        "last_termination": endings[-1].get("reason") if endings else None,
    }
