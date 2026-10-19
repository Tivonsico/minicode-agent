"""Task state is a checkpoint, not a second memory or a copy of the transcript."""

import copy
import uuid

from backend.modes.catalog import IDENTITY_FIELDS, tags
from backend.storage.nonblocking import checkpoint_io


def empty_state():
    return {
        "status": "idle",
        "objective": "",
        "turn_id": None,
        "request_count": 0,
        "context_estimate": 0,
        "pending_tool": None,
        "phase": "idle",
        "step_id": None,
        "notes": "",
        "next_step": "",
        "verification_status": "not_run",
        **dict.fromkeys(IDENTITY_FIELDS),
    }


def reduce_state(state, event):
    kind = event["type"]
    if kind in ("checkpoint", "task_state") and isinstance(event.get("state"), dict):
        state.update(event["state"])
    elif kind == "run_accepted" and isinstance(event.get("input"), dict):
        state = {
            **empty_state(),
            "objective": event["input"].get("content", ""),
            "status": "running",
            "phase": "starting",
            "turn_id": event.get("turn_id"),
            **tags(event),
        }
    elif kind == "turn_start" and event.get("turn_id") != state["turn_id"]:
        state = {
            **empty_state(),
            "turn_id": event.get("turn_id"),
            "status": "running",
            "phase": "starting",
            **tags(event),
        }
    elif kind == "message" and event.get("role") == "user":
        state.update(objective=event.get("content", ""))
    elif kind in ("turn_end", "run_end") and event.get("turn_id") == state["turn_id"]:
        state.update(
            status=event.get("status", "interrupted"),
            phase="ended",
            pending_tool=None,
            **tags(event),
        )
    return state


class Checkpoints:
    def __init__(self, store):
        self.store = store
        checkpoint = store.restored_checkpoint
        self._state = {**empty_state(), **(checkpoint.get("state", {}) if checkpoint else {})}
        self._cursor = store.recovery_info["covered_seq"]
        self.revision = checkpoint.get("seq", 0) if checkpoint else 0
        self._sync()

    def _sync(self):
        for event in self.store.events_since(self._cursor):
            self._state = reduce_state(self._state, event)
            if event["type"] in ("checkpoint", "task_state"):
                self.revision = event["seq"]
            self._cursor = event["seq"]

    @checkpoint_io
    def begin(self, objective, turn_id):
        self._sync()
        self.update(
            **{
                **empty_state(),
                "objective": objective,
                "turn_id": turn_id,
                "status": "running",
                "phase": "starting",
                **tags(self._state),
            }
        )

    @checkpoint_io
    def update(self, *, source="automatic", **changes):
        self._sync()
        if changes.keys() - self._state.keys():
            raise ValueError("Unknown checkpoint state fields")
        return self.capture(source, state={**self._state, **changes})

    @checkpoint_io
    def snapshot(self):
        self._sync()
        return copy.deepcopy(self._state)

    @checkpoint_io
    def capture(self, source="automatic", *, state=None, **identities):
        self._sync()
        candidate = copy.deepcopy(self._state if state is None else state)
        latest = next(
            (e for e in reversed(self.store.view) if e["type"] == "context_compressed"), None
        )
        record = self.store.append(
            "checkpoint",
            checkpoint_id=uuid.uuid4().hex,
            source=source,
            schema_version=1,
            reducer_version=1,
            covered_seq=self.store.seq,
            state=candidate,
            context_seq=latest["seq"] if latest else None,
            **{
                **tags(candidate),
                "turn_id": candidate.get("turn_id"),
                "step_id": candidate.get("step_id"),
                **identities,
            },
        )
        self._state, self._cursor, self.revision = copy.deepcopy(record["state"]), record["seq"], record["seq"]
        self.store.save_checkpoint(record)
        return record

    @checkpoint_io
    def edit(self, expected_revision, changes, is_running):
        self._sync()
        if expected_revision != self.revision:
            raise ValueError("任务状态已变化，请刷新后再保存")
        running = is_running()
        if running and changes.get("status") != "running":
            raise ValueError("任务仍在执行，状态必须保持执行中；停止任务请使用停止按钮")
        if not running and changes.get("status") == "running":
            raise ValueError("没有执行中的任务，不能将状态标为执行中")
        self.update(source="manual", **changes)
        return {"state": self.snapshot(), "state_revision": self.revision}

    @checkpoint_io
    def listing(self):
        return [e for e in self.store.events if e["type"] == "checkpoint"]
