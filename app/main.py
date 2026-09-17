import argparse
import asyncio
from pathlib import Path

from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

from config import ConfigError, load_config
from memory.project_lock import ProjectLock


def main():
    parser = argparse.ArgumentParser(description="minicode 项目终端编程助手")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="项目根目录")
    parser.add_argument("--check-config", action="store_true", help="只验证本地配置，不请求模型")
    args = parser.parse_args()
    try:
        config = load_config(args.project)
        if args.check_config:
            print(
                f"配置有效：模型 {config.model}，输入预算 {config.context_window - config.max_output_tokens}。尚未验证服务端可用性。"
            )
            return 0
        from app.terminal import TerminalApp

        with ProjectLock(config.root):
            asyncio.run(TerminalApp(config).run())
        return 0
    except (ConfigError, RuntimeError, OSError, ValueError) as exc:
        print(f"minicode: {exc}")
        return 1
    except NoConsoleScreenBufferError:
        print(
            "minicode: 未检测到 Windows 控制台。请在 Windows Terminal 或 cmd 的交互窗口中启动，"
            "不要把启动输出重定向到文件或由非交互任务运行。"
        )
        return 1
    except KeyboardInterrupt:
        return 130
