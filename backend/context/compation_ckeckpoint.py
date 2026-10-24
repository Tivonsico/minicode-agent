"""Persist only a bounded prepared-history cache; the raw journal stays authoritative."""

import copy
import hashlib
import json

from backend.conversation.snapshot import runtime_events
from backend.modes.catalog import tags


def digest(messages):
    return hashlib.sha256(
        json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class CompactionCheckpoint:
    def __init__(self, store):
        self.store = store
        self.record = next(
            (
                event
                for event in reversed(runtime_events(store))
                if event["type"] == "context_compressed" and "source_digest" in event
            ),
            None,
        )

    def restore(self, body):
        if not self.record:
            return None
        count = self.record["source_length"]
        if len(body) >= count and digest(body[:count]) == self.record["source_digest"]:
            return copy.deepcopy(body[:count]), copy.deepcopy(self.record["prepared_history"])
        return None

    def save(self, source, prepared, estimate):
        events = runtime_events(self.store)
        messages = [e for e in events if e["type"] == "message"]
        record = {
            "source_modes": sorted({e.get("agent_mode") or "legacy_unknown" for e in messages}),
            "source_profiles": list(
                {
                    e.get("turn_id"): {
                        "turn_id": e.get("turn_id"),
                        "run_id": e.get("run_id"),
                        **tags(e),
                    }
                    for e in messages
                }.values()
            ),
            "source_first_seq": messages[0]["seq"] if messages else None,
            "source_last_seq": messages[-1]["seq"] if messages else None,
            "source_digest": digest(source),
            "source_length": len(source),
            "prepared_history": copy.deepcopy(prepared),
            "input_estimate": estimate,
        }
        self.store.append("context_compressed", **record)
        self.record = record
