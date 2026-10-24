"""Run-wide cooperative workspace locks and cancellation-safe file I/O."""

import asyncio
import hashlib
import os
from contextlib import AsyncExitStack, asynccontextmanager

from backend.execution.gate import execution_gate


def outer_roots(roots):
    return tuple(sorted((p for p in set(roots) if not any(
        p != other and p.is_relative_to(other) for other in roots)), key=str))


@asynccontextmanager
async def workspace_history_gate(roots):
    # Shared ancestor locks allow unrelated projects to run concurrently, while
    # an exclusive root lock excludes equal or nested projects across app roots.
    roots = outer_roots(roots)
    locks = {}
    for root in roots:
        for path in (root, *root.parents):
            if path == path.parent:
                continue
            locks[path] = locks.get(path, True) and path != root
    async with AsyncExitStack() as stack:
        for path in sorted(locks, key=lambda p: (len(p.parts), str(p))):
            name = "history-" + hashlib.sha256(os.fsencode(path)).hexdigest() + ".lock"
            await stack.enter_async_context(execution_gate(reconciliation=True,
                                                           shared=locks[path], lock_name=name))
        yield


async def file_io(function, *args, **kwargs):
    # Wait for real completion before allowing cancellation to release a lock.
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()
        raise
