from .base import ToolError


async def execute(executor, name, cid, args):
    path = executor.policy.resolve(args["path"], output_read=True)
    executor._touch_project(path)
    if not path.is_file():
        raise ToolError("Not an existing regular file.", "not_file")
    start, count = args.get("start_line", 1), args.get("line_count", 200)
    (before, content, total) = await executor._in_worker(
        executor._read_range, path, start, count
    )
    if executor.policy.resolve(args["path"], output_read=True) != path:
        raise ToolError("File changed during reading; retry.", "stale_read")
    executor.reads[path] = before
    executor.verify_text_read(path, before, start, count, total)
    return content
