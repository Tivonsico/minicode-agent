"""Launch minicode on Windows, macOS or Linux using Python."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
REQUIRED_MODULES = ("dotenv", "httpx", "jsonschema", "fastapi", "uvicorn", "psutil", "pymysql", "greenback")
BOOTSTRAP_FLAG = "MINICODE_LAUNCH_BOOTSTRAPPED"


def main(argv=None, *, stop=False):
    args = list(sys.argv[1:] if argv is None else argv)
    entry = PROJECT_ROOT / "stop.py" if stop else Path(__file__).resolve()
    if sys.version_info < (3, 11):
        print("minicode: 请使用 Python 3.11 或更高版本启动。")
        return 1

    environment = PROJECT_ROOT / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        # Re-enter this file so imports work even when launched outside the repository.
        if python.is_file() and Path(sys.prefix).resolve() != environment.resolve():
            return subprocess.run([str(python), str(entry), *args]).returncode

        missing = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
        if missing:
            uv = shutil.which("uv")
            if uv and not os.environ.get(BOOTSTRAP_FLAG):
                return subprocess.run(
                    [
                        uv,
                        "run",
                        "--project",
                        str(PROJECT_ROOT),
                        "python",
                        str(entry),
                        *args,
                    ],
                    env={**os.environ, BOOTSTRAP_FLAG: "1"},
                ).returncode
            print("minicode: 缺少运行依赖：" + ", ".join(missing))
            print("请在项目目录运行 uv sync，或 python -m pip install -e . 后重新启动。")
            return 1

        if stop:
            from app.stop import main as run_app
        else:
            from app.main import main as run_app

        # Explicit user arguments come last, so --project can override this default.
        return run_app(["--project", str(PROJECT_ROOT), *args])
    except OSError as exc:
        print(f"minicode: 无法启动 Python 环境：{exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
