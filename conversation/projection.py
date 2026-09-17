"""Project completed messages exactly once; repair unmatched calls explicitly."""

import json


def project_messages(events: list[dict]) -> list[dict]:
    messages = []
    seen = set()
    pending = {}

    def flush_pending():
        for call_id in list(pending):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(
                        {
                            "status": "unknown",
                            "message": "执行中断或结果缺失；不得自动重跑，请先检查现状。",
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        pending.clear()

    for event in events:
        if event["type"] == "message":
            message_id = event["message_id"]
            if message_id in seen:
                continue
            seen.add(message_id)
            flush_pending()
            message = {
                key: event[key]
                for key in ("role", "content", "tool_calls", "reasoning_content")
                if key in event
            }
            if event.get("status") == "partial":
                message.pop("tool_calls", None)
                message["content"] = message.get("content", "") + "\n[上次回复未完整结束]"
            messages.append(message)
            for call in message.get("tool_calls", []):
                pending[call["id"]] = True
        elif event["type"] == "tool_result":
            call_id = event.get("call_id", event.get("tool_call_id"))
            if call_id in pending:
                messages.append(
                    {"role": "tool", "tool_call_id": call_id, "content": event.get("content", "")}
                )
                pending.pop(call_id)
    flush_pending()
    return messages
