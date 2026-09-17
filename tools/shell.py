"""Shell entry point. There is deliberately no ordinary subprocess fallback."""

from .base import ToolError

UTF8_PRELUDE = (
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
    "$OutputEncoding = [Console]::OutputEncoding; "
)


async def run_shell(
    root, command, timeout, output_store, call_id, emit, executable=None, *, backend=None
):
    if backend is None or not backend.available():
        raise ToolError("Shell requires a verified Windows sandbox; execution is unavailable.", "shell_unavailable")
    return await backend.run(
        root=root,
        command=command,
        timeout=timeout,
        output_store=output_store,
        call_id=call_id,
        emit=emit,
        executable=executable,
    )
