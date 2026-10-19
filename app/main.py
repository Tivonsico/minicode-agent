"""Cross-platform local web launcher, also available as `python -m app`."""

import argparse
import threading
import webbrowser
from contextlib import nullcontext
from pathlib import Path

from backend.config import ConfigError, load_config
from backend.models import resolve_config
from backend.storage.project_lock import ProjectLock


def main(argv=None):
    parser = argparse.ArgumentParser(description="minicode 本地网页编程助手")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="应用数据和配置目录")
    parser.add_argument("--check-config", action="store_true", help="只验证本地配置，不请求模型")
    parser.add_argument("--check-model", action="store_true", help="解析模型窗口，不生成回复")
    parser.add_argument("--refresh-model", action="store_true", help="忽略缓存，重新解析模型参数")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.project)
        if args.check_config:
            print(f"配置有效：模型 {config.model}。启动时自动解析窗口；尚未验证服务端可用性。")
            return 0
        if not 1 <= args.port <= 65535:
            raise ConfigError("端口必须在 1–65535 之间")
        if not args.check_model:
            from backend.runtime.process import stop

            if stop(config.root):
                print("已终止旧进程，正在重新启动……", flush=True)
        with nullcontext() if args.check_model else ProjectLock(config.root):
            print("正在解析模型参数……", flush=True)
            config = resolve_config(config, refresh=args.refresh_model)
            print(
                f"模型 {config.model}：窗口 {config.context_window}，"
                f"输出预留 {config.max_output_tokens}；来源 {config.capability_source or '调用方提供'}。",
                flush=True,
            )
            if args.check_model:
                print("模型参数已解析；尚未验证生成回复或工具调用能力。")
                return 0
            import uvicorn

            from backend.api.app import create_app

            app = create_app(config)
            url = f"http://127.0.0.1:{args.port}"
            print(f"minicode: {url}；仅监听本机，按 Ctrl+C 关闭后端", flush=True)
            if not args.no_browser:
                opener = threading.Timer(2, webbrowser.open, args=(url,))
                opener.daemon = True
                opener.start()
            uvicorn.run(
                app,
                host="127.0.0.1",
                port=args.port,
                log_level="warning",
                timeout_graceful_shutdown=5,
            )
        return 0
    except (ConfigError, RuntimeError, OSError, ValueError) as exc:
        print(f"minicode: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130
