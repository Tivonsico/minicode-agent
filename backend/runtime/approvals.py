"""Run-bound, single-use approval tickets independent of browser connections."""

import asyncio
import time
import uuid

from backend.modes.catalog import IDENTITY_FIELDS
from backend.observability.scope import current, scope


class ApprovalBroker:
    def __init__(self, publish, checkpoints, lifetime=600):
        self.publish, self.checkpoints, self.lifetime = publish, checkpoints, lifetime
        self.pending = {}
        self.run_id = None

    async def ask(self, request):
        ticket = uuid.uuid4().hex
        with scope(self.checkpoints.store, approval_id=ticket):
            return await self._ask(request, ticket)

    async def _ask(self, request, ticket):
        started = time.monotonic()
        future = asyncio.get_running_loop().create_future()
        item = {**current(self.checkpoints.store), **request, "id": ticket, "run_id": self.run_id}
        outcome = "cancelled"
        self.pending[ticket] = (item, future)
        self.checkpoints.update(pending_tool={"ticket": ticket, "name": item["name"]})
        self.publish("approval_requested", **item)
        try:
            approved = await asyncio.wait_for(future, self.lifetime)
            outcome = "approved" if approved else "rejected"
            return approved
        except TimeoutError:
            outcome = "expired"
            return False
        finally:
            self.pending.pop(ticket, None)
            self.checkpoints.update(pending_tool=None)
            self.publish(
                "approval_closed",
                id=ticket,
                call_id=item.get("call_id"),
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                outcome=outcome,
            )

    def resolve(self, ticket, run_id, approved):
        entry = self.pending.get(ticket)
        if not entry or entry[0]["run_id"] != run_id or entry[1].done():
            raise ValueError("审批已失效，请刷新当前任务")
        item = entry[0]
        self.publish(
            "approval_decision",
            id=ticket,
            approval_id=ticket,
            **{
                key: item.get(key)
                for key in (
                    "run_id",
                    "turn_id",
                    "step_id",
                    "request_id",
                    "call_id",
                    *IDENTITY_FIELDS,
                )
            },
            approved=approved,
        )
        entry[1].set_result(approved)

    def listing(self):
        return [item for item, future in self.pending.values() if not future.done()]
