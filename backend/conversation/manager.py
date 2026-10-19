import uuid

from backend.conversation.projection import project_messages
from backend.observability.scope import current


class ConversationManager:
    def __init__(self, store):
        self.store = store
        self.turn_id = None
        self.active = False

    def start(self, text: str):
        if self.active:
            raise RuntimeError("当前轮仍在执行")
        self.turn_id = current(self.store).get("turn_id") or uuid.uuid4().hex
        self.store.append("turn_start", turn_id=self.turn_id, status="running")
        self.active = True
        accepted = next(
            (
                e
                for e in reversed(self.store.view)
                if e["type"] == "run_accepted" and e.get("turn_id") == self.turn_id
            ),
            {},
        )
        fields = accepted.get("input", {})
        return self.message(
            "user",
            fields.get("content", text),
            message_id=fields.get("message_id", uuid.uuid4().hex),
        )

    def message(self, role: str, content: str, **fields):
        return self.store.append(
            "message",
            turn_id=self.turn_id,
            message_id=fields.pop("message_id", uuid.uuid4().hex),
            role=role,
            content=content,
            status=fields.pop("status", "complete"),
            is_final=fields.pop("is_final", False),
            **fields,
        )

    def finish(self, status: str, reason: str):
        if self.active:
            self.store.append("turn_end", turn_id=self.turn_id, status=status, reason=reason)
            self.active = False

    def messages(self):
        return project_messages(self.store.view)
