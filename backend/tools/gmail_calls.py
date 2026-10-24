"""Email approval binds to an immutable durable request, never model-edited content."""
import asyncio
import json

from backend.gmail import GmailService

from .base import ToolResult


async def execute(executor, name, cid, args):
    if not getattr(executor, "gmail", None):
        executor.gmail = GmailService(executor.root)
    gmail = executor.gmail
    if name == "GmailPrepareEmail":
        result = gmail.prepare(**args)
    elif name == "GmailEmailStatus":
        result = gmail.status(args["request_id"])
    else:
        result = gmail.status(args["request_id"])
        if result["status"] == "prepared":
            details = json.dumps(result, ensure_ascii=False, indent=2)
            if not await executor._approval(
                cid, name, args, "通过 Gmail 发送邮件（外部发送）", details=details
            ):
                return ToolResult(cid, name, "rejected", "User rejected sending this email.")
            executor.mark_running(cid, name)
            try:
                result = await asyncio.to_thread(gmail.send, args["request_id"])
            except asyncio.CancelledError as error:
                error.tool_status = "unknown"
                error.tool_content = (
                    "Gmail sending may continue. Do not resend. Check GmailEmailStatus with request_id="
                    + args["request_id"]
                )
                raise
    return json.dumps(result, ensure_ascii=False)
