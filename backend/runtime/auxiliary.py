from backend.observability.requests import recorded_request
from backend.observability.scope import current, scope


async def ignore_delta(*args, **kwargs):
    pass


async def auxiliary_request(provider, store, messages, purpose):
    with scope(
        store,
        source_step_id=current(store).get("step_id"),
        step_id=None if purpose == "memory" else current(store).get("step_id"),
        message_id=None,
        request_id=None,
        call_id=None,
        approval_id=None,
    ):
        return await recorded_request(provider, store, messages, [], ignore_delta, purpose=purpose)
