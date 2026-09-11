"""无需 MCP 客户端的命令行诊断与调用。标准输出仅写 JSON。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tomllib

from .config import load_config
from .models import MediaMcpError
from .service import MediaService


def parser():
    root = argparse.ArgumentParser(description="Media Hunter 搜索、下载与连接诊断")
    root.add_argument("--config", help="TOML 配置文件路径")
    commands = root.add_subparsers(dest="operation", required=True)
    commands.add_parser("check", help="检查站点连通性和凭证")
    for name in ("search", "download-search"):
        command = commands.add_parser(name)
        command.add_argument("site", choices=["e621", "rule34", "ehentai", "pixiv"])
        command.add_argument("query")
        command.add_argument("--limit", type=int, default=10)
        command.add_argument("--rating")
        command.add_argument("--min-score", type=float)
        if name == "search":
            command.add_argument("--page", type=int, default=1)
        else:
            command.add_argument("--subdir")
            command.add_argument("--overwrite", action="store_true", help="强制重新下载已有文件")
        command.add_argument("--timeout", type=float)
    for name in ("get", "download"):
        command = commands.add_parser(name)
        command.add_argument("site", choices=["e621", "rule34", "ehentai", "pixiv"])
        command.add_argument("post_id")
        command.add_argument("--timeout", type=float)
        if name == "download":
            command.add_argument("--subdir")
            command.add_argument("--overwrite", action="store_true", help="强制重新下载已有文件")
    command = commands.add_parser("download-url")
    command.add_argument("url")
    command.add_argument("--subdir")
    command.add_argument("--timeout", type=float)
    command.add_argument("--overwrite", action="store_true", help="强制重新下载已有文件")
    return root


async def run(args):
    options = vars(args).copy()
    service = MediaService(load_config(options.pop("config")))
    operation = options.pop("operation")
    operation = {"check": "self_check", "get": "get_post", "download": "download_post"}.get(
        operation, operation.replace("-", "_")
    )
    try:
        result = await service.execute(operation, **options)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if operation == "self_check":
            return (
                0
                if all(result.get("data", {}).get(site, {}).get("ok") for site in service.adapters)
                else 1
            )
        return 0 if result["success"] else 1
    finally:
        await service.close()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:
        if isinstance(exc, MediaMcpError):
            error = {**exc.to_dict(), "type": "config"}
        elif isinstance(exc, tomllib.TOMLDecodeError):
            error = {"type": "config", "message": "TOML 配置语法错误", "hint": str(exc)}
        elif isinstance(exc, FileNotFoundError):
            error = {
                "type": "config",
                "message": "找不到配置文件",
                "hint": "检查 --config 或 MEDIA_HUNTER_CONFIG 指向的路径",
            }
        else:
            error = {
                "type": "config",
                "message": f"启动失败（{type(exc).__name__}），请检查配置文件与依赖",
            }
        print(
            json.dumps(
                {
                    "success": False,
                    "error": error,
                },
                ensure_ascii=False,
            )
        )
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
