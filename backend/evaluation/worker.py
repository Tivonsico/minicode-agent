"""Standalone candidate adapter, run only inside a read-only OS sandbox.

No expected answers are given to this process. The parent grades returned values.
"""

import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path


def main():
    work = Path(sys.argv[1])
    # Candidate projects may import their own helper modules inside the sandbox.
    sys.path.insert(0, str(work))
    checks = json.loads(Path(sys.argv[2]).read_text())
    modules, results = {}, []
    for check in checks:
        try:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                path = check["path"]
                if path not in modules:
                    spec = importlib.util.spec_from_file_location("candidate", work / path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    modules[path] = module
                function = getattr(modules[path], check["symbol"])
                value = function(*check["args"], **check["kwargs"])
                # Enforce plain JSON output, never pickle an untrusted object.
                encoded = json.dumps(value, allow_nan=False)
            results.append({"value": json.loads(encoded)})
        except BaseException as exc:
            results.append({"exception": type(exc).__name__})
    print(json.dumps(results, allow_nan=False))


if __name__ == "__main__":
    main()
