import asyncio
import threading
from pathlib import Path

from .base import ToolError


async def execute(executor, name, cid, args):
    executor.policy.base()
    executor.touched_projects.update(executor.policy.roots())
    pattern = args["pattern"] if name == "Glob" else args.get("glob", "**/*")
    if ".." in Path(pattern).parts or Path(pattern).is_absolute():
        raise ToolError("Search pattern must stay within work directories.", "path")
    stop = threading.Event()
    grant_version = executor.session_permissions.version if executor.session_permissions else None
    try:
        content = await asyncio.wait_for(
            asyncio.to_thread(executor._search_files, name, pattern, args.get("pattern"), stop),
            timeout=8,
        )
    except TimeoutError as error:
        raise ToolError("Search timed out; use a narrower pattern.", "search_limit") from error
    finally:
        stop.set()
    if executor.session_permissions and executor.session_permissions.version != grant_version:
        raise ToolError("Permissions changed during search; retry.", "stale_read")
    return content
