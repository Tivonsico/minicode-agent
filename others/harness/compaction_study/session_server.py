"""An ordinary MiniCode web instance with an explicit experimental input cap."""

import argparse
from dataclasses import replace
from pathlib import Path

import uvicorn

from backend.api.app import create_app
from backend.config import load_config
from backend.evaluation.attempt import EmptyMemory
from backend.models import resolve_config


class IsolatedMemory(EmptyMemory):
    """Same isolation as the existing benchmark; history compaction stays original."""

    def list(self):
        return []

    async def close(self):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--input-cap", type=int, default=12000)
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--long-memory", choices=["enabled", "disabled"], default="disabled")
    parser.add_argument("--history-recall", action="store_true")
    args = parser.parse_args()
    original = resolve_config(load_config())
    root = Path(args.runtime_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    config = replace(original, root=root, storage_mode="local", max_output_tokens=4096,
                     model_max_input=args.input_cap,
                     experimental_history_recall=args.history_recall,
                     capability_source=original.capability_source + "; experiment input cap")
    print(f"MiniCode normal sessions: http://127.0.0.1:{args.port}; "
          f"input cap={args.input_cap}; model={config.model}", flush=True)
    app = create_app(config)
    if args.long_memory == "disabled":
        app.state.service.long_memory = IsolatedMemory()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
