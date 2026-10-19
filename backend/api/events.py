"""Replayable SSE stream. Observers never own the task lifetime."""

import asyncio
import json

from fastapi.responses import StreamingResponse


def event_response(session, after, request=None):
    async def stream():
        from backend.storage.nonblocking import portal
        await portal(session.store)
        cursor = after
        idle_seconds = 0
        while request is None or not await request.is_disconnected():
            session.changed.clear()
            pending = (
                session.store.events_since(cursor)
                if cursor >= session.store.recovery_info["covered_seq"]
                else [event for event in session.store.events if event["seq"] > cursor]
            )
            for event in pending:
                cursor = event["seq"]
                yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            try:
                await asyncio.wait_for(session.changed.wait(), timeout=1 if request else 15)
            except TimeoutError:
                idle_seconds += 1 if request else 15
                if idle_seconds >= 15:
                    yield ": heartbeat\n\n"
                    idle_seconds = 0

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )
