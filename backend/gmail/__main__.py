"""Human-only OAuth bootstrap, independent of model/provider configuration."""
import argparse
from pathlib import Path

from backend.tools.base import ToolError

from .google_api import connect, token_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="连接 minicode 的 Gmail 官方 API 工具")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="minicode 应用数据目录")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("connect", help="在浏览器中授权 Gmail 发送权限")
    setup.add_argument("--credentials", type=Path, required=True, help="Google 桌面 OAuth 客户端 JSON")
    commands.add_parser("status", help="查看本机是否已经保存授权")
    commands.add_parser("disconnect", help="删除本机令牌；请先停止 minicode")
    args = parser.parse_args(argv)
    try:
        if args.command == "connect":
            connect(args.project, args.credentials)
            print("Gmail 已连接。启动 minicode 后可在聊天中要求发送邮件；发送前会展示审批。")
        elif args.command == "disconnect":
            from backend.storage.files import sync_directory

            path = token_path(args.project)
            path.unlink(missing_ok=True)
            sync_directory(path.parent)
            print("本机 Gmail 令牌已删除。Google 账户中的授权可在账户安全设置中撤销。")
        else:
            print("已保存 Gmail 授权（尚未验证远端有效性）" if token_path(args.project).exists() else "Gmail 尚未连接")
        return 0
    except ToolError as error:
        print(f"Gmail 连接失败：{error}")
        return 1
    except Exception as error:
        print(f"Gmail 连接失败：{type(error).__name__}。请检查客户端文件、网络或重新授权。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
