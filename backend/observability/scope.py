"""Immutable task-local identities; background jobs retain their source run."""

from contextlib import contextmanager
from contextvars import ContextVar

_active = ContextVar("execution_scope", default=None)


def current(store=None):
    value = _active.get()
    if value is None or (store is not None and value[0] is not store):
        return {}
    return dict(value[1])


def active_store():
    value = _active.get()
    return value[0] if value else None


@contextmanager
def scope(store, **identities):
    values = {**current(store), **identities}
    token = _active.set((store, values))
    try:
        yield values
    finally:
        _active.reset(token)
