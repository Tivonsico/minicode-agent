"""Bounded shutdown entry point; no model or configuration reads."""

import argparse
from pathlib import Path

from backend.runtime.process import stop


def main(argv=None):
    parser = argparse.ArgumentParser(description="终止当前项目的 minicode 进程")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        stopped = stop(args.project)
        print(
            "minicode: 已终止当前项目进程。" if stopped else "minicode: 当前项目没有运行中的进程。",
            flush=True,
        )
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"minicode: {error}", flush=True)
        return 1
