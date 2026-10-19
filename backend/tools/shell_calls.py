from .base import ToolError, ToolResult
from .command_guard import check_command
from .shell import run_shell


async def execute(executor, name, cid, args):
    check_command(args["command"], executor.policy.roots())
    if executor.shell_backend is None or not executor.shell_backend.available():
        raise ToolError(
            "Shell requires a verified Codex OS sandbox; execution is unavailable.",
            "shell_unavailable",
        )
    grant_version = executor.session_permissions.version if executor.session_permissions else None
    timeout = args.get("timeout", 120)
    preview = args["command"].splitlines()[0][:120]
    if len(args["command"]) > 120 or "\n" in args["command"]:
        preview += "…"
    roots = "\n".join((str(path) for path in executor.policy.roots()))
    if not await executor._approval(
        cid,
        name,
        args,
        f"Shell: {preview}\nWorking directory: {executor.policy.base()}\nAuthorized directories:\n{roots}\nTimeout: {timeout}s; network disabled.\nOnly authorized workspace access is available in the OS sandbox.",
        details=args["command"],
    ):
        return ToolResult(cid, name, "rejected", "User rejected this operation.")
    if executor.session_permissions and executor.session_permissions.version != grant_version:
        raise ToolError("Permissions changed after approval.", "stale_approval")
    executor.mark_running(cid, name)
    (status, content, reason) = await run_shell(
        executor.policy.base(),
        args["command"],
        timeout,
        executor.output_store,
        cid,
        executor.emit,
        executable=executor.shell_executable,
        backend=executor.shell_backend,
    )
    if reason == "storage_error":
        (executor.terminal_failed, executor.pause_reason) = (True, reason)
    if args.get("purpose") == "verification":
        executor.emit(
            "verification_result",
            call_id=cid,
            source="execution",
            command=args["command"],
            status="passed"
            if status == "succeeded"
            else "failed"
            if status == "failed"
            else "inconclusive",
            content=content,
            error_type=reason,
        )
    # Editing still compares the live fingerprint with the original read.
    # A Shell inspection does not invalidate unchanged file evidence.
    content = executor.output_store.preview(cid, content)
    return ToolResult(cid, name, status, content, reason)
