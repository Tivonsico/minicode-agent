import copy

from .budget import estimate, input_budget
from .checkpoint import ContextCheckpoint
from .compactor import summarize


class ContextOverflow(RuntimeError):
    pass


def groups(messages):
    """Keep a tool-calling assistant and all its results indivisible."""
    result = []
    for message in messages:
        if message.get("role") == "tool" and result:
            result[-1].append(message)
        else:
            result.append([message])
    return result


def valid_tools(messages):
    pending = set()
    for message in messages:
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                return False
            pending.remove(call_id)
        else:
            if pending:
                return False
            pending = {c["id"] for c in message.get("tool_calls", [])}
    return not pending


class ContextManager:
    def __init__(self, config, provider, store):
        self.config, self.provider, self.store = config, provider, store
        self.last_estimate = 0
        self._source_body, self._compressed_body = [], []
        self.checkpoint = ContextCheckpoint(store)

    async def prepare(self, messages, tools):
        original = copy.deepcopy(messages)
        system = [m for m in original if m.get("role") in ("system", "developer")]
        body = [m for m in original if m.get("role") not in ("system", "developer")]
        raw_body = copy.deepcopy(body)
        if not self._source_body:
            restored = self.checkpoint.restore(body)
            if restored and valid_tools(restored[1]):
                self._source_body, self._compressed_body = restored
        if self._source_body and body[: len(self._source_body)] == self._source_body:
            body = copy.deepcopy(self._compressed_body) + body[len(self._source_body) :]
        current = system + body
        budget = input_budget(self.config)
        self.last_estimate = estimate(current, tools)
        if self.last_estimate < 0.8 * budget:
            return current
        grouped = groups(body)
        latest_user = max(
            (i for i, g in enumerate(grouped) if g[0].get("role") == "user"), default=0
        )
        selected = {i for i in range(max(0, len(grouped) - 4)) if i != latest_user}
        old = [m for i, g in enumerate(grouped) if i in selected for m in g]
        recent = [m for i, g in enumerate(grouped) if i not in selected for m in g]
        if old:
            try:
                summary = await summarize(self.provider, self.store, old, budget)
                candidate = system + [{"role": "user", "content": summary}] + recent
                candidate_size = estimate(candidate, tools)
                if (
                    candidate_size <= budget
                    and candidate_size < self.last_estimate
                    and valid_tools(candidate)
                ):
                    self._source_body = raw_body
                    self._compressed_body = copy.deepcopy(candidate[len(system) :])
                    self.last_estimate = candidate_size
                    self.checkpoint.save(raw_body, self._compressed_body, candidate_size)
                    return candidate
            except Exception:
                self.store.append(
                    "context_compression_error",
                    reason="Compression failed; original context retained",
                )
        if self.last_estimate > budget:
            self.store.append(
                "context_overflow", input_estimate=self.last_estimate, input_budget=budget
            )
            raise ContextOverflow(
                "Context exceeds input budget; history saved. Compression could not safely reduce it."
            )
        return current
