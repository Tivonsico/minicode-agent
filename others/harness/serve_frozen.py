"""Serve the frozen Minicode source without replacing the working tree."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".minicode/swe-bench/campaign-25-source"))

import uvicorn  # noqa: E402

from backend.api.app import create_app  # noqa: E402
from backend.config import load_config  # noqa: E402
from backend.models import resolve_config  # noqa: E402
from backend.runtime.process import stop  # noqa: E402
from backend.storage.project_lock import ProjectLock  # noqa: E402


def main():
    stop(ROOT)
    with ProjectLock(ROOT):
        config = resolve_config(load_config(ROOT))
        print("Serving frozen baseline on http://127.0.0.1:8765", flush=True)
        uvicorn.run(create_app(config), host="127.0.0.1", port=8765, log_level="warning")


if __name__ == "__main__":
    main()
