"""Actual sandbox probe, separate from all SWE-bench model scores."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(sys.argv[1]).resolve()
DESTINATION = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(SOURCE))

from types import SimpleNamespace  # noqa: E402

from backend.context.prompts import assemble_system_prompt  # noqa: E402
from backend.conversation.store import SessionStore  # noqa: E402
from backend.execution.codex import CodexSandbox  # noqa: E402
from backend.permissions.directories import SessionPermissions  # noqa: E402
from backend.storage.output_store import OutputStore  # noqa: E402
from backend.tools.executor import ToolExecutor  # noqa: E402


def call(name, arguments, identifier):
    return {
        "id": identifier,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


async def main():
    records = []
    with tempfile.TemporaryDirectory(prefix="minicode-read-probe-") as folder:
        directory = Path(folder).resolve()
        app, work = directory / "app", directory / "work"
        app.mkdir()
        work.mkdir()
        store = SessionStore(app)
        grants = SessionPermissions(app, store)
        grants.add(str(work))

        async def approve(_request):
            return True

        executor = ToolExecutor(app, OutputStore(store), approve, session_permissions=grants)
        executor.agent_mode = "auto"
        executor.begin_turn(str(work))
        assemble_system_prompt(executor, SimpleNamespace(snapshot=lambda: []))
        executor.shell_backend = CodexSandbox(app, grants.roots)
        assert await executor.shell_backend.verify()
        for changed in (False, True):
            target = work / "state.py"
            target.write_text("x = 1\n")
            read = await executor.execute_batch(
                [call("ReadFile", {"path": str(target)}, "read-" + str(changed))]
            )
            assert read[0].status == "succeeded", read[0].content
            shell = "printf 'x = 3\\n' > state.py" if changed else "printf inspection"
            result = await executor.execute_batch(
                [call("Shell", {"command": shell}, "shell-" + str(changed))]
            )
            assert result[0].status == "succeeded", result[0].content
            edit = await executor.execute_batch(
                [
                    call(
                        "EditFile",
                        {
                            "path": str(target),
                            "old_text": "x = 1",
                            "new_text": "x = 2",
                        },
                        "edit-" + str(changed),
                    )
                ]
            )
            records.append(
                {
                    "shell_changed_file": changed,
                    "edit_status": edit[0].status,
                    "error_type": edit[0].error_type,
                    "final_content": target.read_text(),
                }
            )
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_text(json.dumps({"source": str(SOURCE), "records": records}, indent=2) + "\n")
    print(json.dumps(records))


if __name__ == "__main__":
    asyncio.run(main())
