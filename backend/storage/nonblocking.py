"""Keep synchronous storage interfaces while database waits yield to asyncio.

Whole store actions run in the database's bounded worker pool. The portal only
bridges existing synchronous callers; it never runs a tool in a DB transaction.
"""
import asyncio
from functools import wraps

import greenback


def io_method(function):
    @wraps(function)
    def call(self, *args, **kwargs):
        database = getattr(self, "database", self)
        def locked():
            target = getattr(self, "store", self)
            lock = getattr(target, "_lock", None)
            if lock is None:
                return function(self, *args, **kwargs)
            with lock:
                return function(self, *args, **kwargs)
        return database.run_io(locked)
    return call


def checkpoint_io(function):
    @wraps(function)
    def call(self, *args, **kwargs):
        database = getattr(self.store, "database", None)
        if database is None:
            return function(self, *args, **kwargs)
        def locked():
            with self.store._lock:
                return function(self, *args, **kwargs)
        return database.run_io(locked)
    return call


async def portal(store):
    if getattr(store, "database", None):
        await greenback.ensure_portal()


def in_portal():
    try:
        asyncio.get_running_loop()
        return greenback.has_portal()
    except RuntimeError:
        return False
