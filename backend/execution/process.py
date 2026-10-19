"""Bound output and settle child processes before releasing the tool barrier."""

import asyncio
import os
import signal
import subprocess

OUTPUT_LIMIT = 2_000_000


class TerminationUnknown(RuntimeError):
    pass


async def stop_tree(process):
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        code = await killer.wait()
        if code and process.returncode is None:
            raise TerminationUnknown("Could not confirm Windows child process termination")
    else:
        # Codex may give its child a separate process group. Include descendants.
        listing = await asyncio.create_subprocess_exec(
            "ps", "-axo", "pid=,ppid=", stdout=asyncio.subprocess.PIPE
        )
        raw, _ = await listing.communicate()
        pairs = [
            tuple(map(int, row.split()))
            for row in raw.decode().splitlines()
            if len(row.split()) == 2
        ]
        descendants = {process.pid}
        for _ in range(len(pairs)):
            new = {pid for pid, parent in pairs if parent in descendants}
            if new <= descendants:
                break
            descendants.update(new)
        for pid in reversed(sorted(descendants)):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await asyncio.wait_for(process.wait(), timeout=5)


async def capture(argv, root, env, timeout):
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=root,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **({"start_new_session": True} if os.name != "nt" else {}),
    )
    buffers = [bytearray(), bytearray()]

    async def drain(stream, target):
        while chunk := await stream.read(8192):
            if len(target) + len(chunk) > OUTPUT_LIMIT:
                raise ValueError("Shell output exceeded 2 MB per stream; process stopped.")
            target.extend(chunk)

    readers = [
        asyncio.create_task(drain(process.stdout, buffers[0])),
        asyncio.create_task(drain(process.stderr, buffers[1])),
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*readers, process.wait()), timeout)
    except BaseException:
        try:
            try:
                await asyncio.shield(stop_tree(process))
            except (OSError, TimeoutError, TerminationUnknown) as exc:
                raise TerminationUnknown("Could not confirm sandbox process termination") from exc
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
        raise
    return process.returncode, *(bytes(value).decode("utf-8", "replace") for value in buffers)
