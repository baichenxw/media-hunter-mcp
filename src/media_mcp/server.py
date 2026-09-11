"""FastMCP 入口。导入模块不加载凭证，生命周期退出时关闭 HTTP 连接。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field

from . import __version__
from .config import Config
from .service import MediaService

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "data": {"type": "object", "additionalProperties": True},
        "error": {
            "type": "object",
            "properties": {key: {"type": "string"} for key in ("type", "message", "hint")},
            "required": ["type", "message"],
            "additionalProperties": False,
        },
    },
    "required": ["success"],
    "additionalProperties": False,
    "oneOf": [
        {"properties": {"success": {"const": True}}, "required": ["data"]},
        {"properties": {"success": {"const": False}}, "required": ["error"]},
    ],
}
READ_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}
DOWNLOAD_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}
PositiveTimeout = Annotated[float, Field(gt=0, allow_inf_nan=False)]


def create_server(config: Config | None = None, service: MediaService | None = None) -> FastMCP:
    instance = service

    def get_service():
        nonlocal instance
        if instance is None:
            instance = MediaService(config)
        return instance

    @asynccontextmanager
    async def lifespan(server):
        try:
            get_service()
            yield {}
        finally:
            if instance is not None:
                await instance.close()

    app = FastMCP(
        "media-hunter-mcp",
        version=__version__,
        lifespan=lifespan,
        strict_input_validation=True,
        cache_ttl=60,
        cache_scope="private",
    )

    async def call(operation, ctx, **kwargs):
        result = await get_service().execute(operation, progress=ctx.report_progress, **kwargs)
        # 保留 JSON 文本及结构化清单，包括部分失败的成功文件。
        return ToolResult(structured_content=result, is_error=not result["success"])

    @app.tool(title="搜索作品", annotations=READ_ANNOTATIONS, output_schema=OUTPUT_SCHEMA)
    async def search(
        site: str,
        query: str,
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=1000)] = 20,
        page: Annotated[int, Field(ge=1)] = 1,
        min_score: float | None = None,
        rating: str | None = None,
        use_exhentai: bool | None = None,
    ) -> ToolResult:
        """搜索 e621/rule34/ehentai/pixiv。query 使用站点原生标签或关键词。
        page 从 1 开始；limit 上限分别为 320/1000/100/30。
        rating：e621 s/q/e；rule34 safe/questionable/explicit；
        pixiv all（不限）/safe/r18/r18g（精确等级）；ehentai 为画廊分类。
        ehentai 的 use_exhentai=true 选里站、false 选表站，省略沿用配置（默认里站）。
        min_score 为站点分数（Pixiv 收藏数、E-Hentai 星级）。过滤后可能少于 limit。
        """
        return await call(
            "search",
            ctx,
            site=site,
            query=query,
            limit=limit,
            page=page,
            min_score=min_score,
            rating=rating,
            use_exhentai=use_exhentai,
        )

    @app.tool(title="获取作品详情", annotations=READ_ANNOTATIONS, output_schema=OUTPUT_SCHEMA)
    async def get_post(
        site: str, post_id: str, ctx: Context, use_exhentai: bool | None = None
    ) -> ToolResult:
        """作品完整元数据。post_id 为数字；ehentai 为 gid/token。
        ehentai 的 use_exhentai=true 选里站、false 选表站，省略沿用配置（默认里站）。
        """
        return await call("get_post", ctx, site=site, post_id=post_id, use_exhentai=use_exhentai)

    @app.tool(title="下载作品", annotations=DOWNLOAD_ANNOTATIONS, output_schema=OUTPUT_SCHEMA)
    async def download_post(
        site: str,
        post_id: str,
        ctx: Context,
        subdir: str | None = None,
        timeout: PositiveTimeout | None = None,
        overwrite: bool = False,
        use_exhentai: bool | None = None,
        original: bool = False,
    ) -> ToolResult:
        """按 ID 下载整部作品，返回文件清单及 sidecar。subdir 为下载根目录下的分组名。
        timeout 为覆盖等待、元数据、解析、下载及合成的总秒数。失败文件不会成为最终文件。
        ehentai 的 use_exhentai=true 选里站、false 选表站，省略沿用配置。
        original=true 下载原图（可能消耗 FIQ/GP），默认 false；须配置 allow_original=true（默认开启）。
        默认跳过已通过清单和 SHA-256 校验的文件；overwrite=true 强制重新下载并替换。
        """
        return await call(
            "download_post",
            ctx,
            site=site,
            post_id=post_id,
            subdir=subdir,
            timeout=timeout,
            overwrite=overwrite,
            use_exhentai=use_exhentai,
            original=original,
        )

    @app.tool(title="搜索并下载", annotations=DOWNLOAD_ANNOTATIONS, output_schema=OUTPUT_SCHEMA)
    async def download_search(
        site: str,
        query: str,
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        min_score: float | None = None,
        rating: str | None = None,
        subdir: str | None = None,
        timeout: PositiveTimeout | None = None,
        overwrite: bool = False,
        use_exhentai: bool | None = None,
        original: bool = False,
    ) -> ToolResult:
        """搜索并批量下载，最多尝试 50 个文件。超额画廊整本跳过，可用 download_post 单独下载。
        返回 downloaded/errors/skipped；timeout 覆盖搜索和下载全流程。部分完成时 success=false，成功文件仍保留。
        ehentai 的 use_exhentai=true 选里站、false 选表站；original=true 下载原图（可能消耗 FIQ/GP）。
        原图须配置 allow_original=true（默认开启），original 默认 false。
        默认校验并复用已完成文件；overwrite=true 强制重新下载。凭证或配额错误停止剩余批次。
        """
        return await call(
            "download_search",
            ctx,
            site=site,
            query=query,
            limit=limit,
            min_score=min_score,
            rating=rating,
            subdir=subdir,
            timeout=timeout,
            overwrite=overwrite,
            use_exhentai=use_exhentai,
            original=original,
        )

    @app.tool(title="按链接下载", annotations=DOWNLOAD_ANNOTATIONS, output_schema=OUTPUT_SCHEMA)
    async def download_url(
        url: str,
        ctx: Context,
        subdir: str | None = None,
        timeout: PositiveTimeout | None = None,
        overwrite: bool = False,
        use_exhentai: bool | None = None,
        original: bool = False,
    ) -> ToolResult:
        """识别四站作品页面 URL 并下载。timeout 为总秒数；不接受任意文件直链。
        ehentai 默认按 URL 域名选表站或里站，也可用 use_exhentai 覆盖。
        ehentai 的 use_exhentai=true 选里站、false 选表站；original=true 下载原图（可能消耗 FIQ/GP）。
        原图须配置 allow_original=true（默认开启），original 默认 false。
        默认校验并复用已完成文件；overwrite=true 强制重新下载并替换。
        """
        return await call(
            "download_url",
            ctx,
            url=url,
            subdir=subdir,
            timeout=timeout,
            overwrite=overwrite,
            use_exhentai=use_exhentai,
            original=original,
        )

    @app.tool(title="检查站点连通性", annotations=READ_ANNOTATIONS, output_schema=OUTPUT_SCHEMA)
    async def self_check(ctx: Context, use_exhentai: bool | None = None) -> ToolResult:
        """并行检查四站凭证及 API 连通性，每站最多 45 秒；不下载媒体、不返回凭证。
        use_exhentai 仅影响 E 站检查：true 里站、false 表站，省略沿用配置。
        """
        return await call("self_check", ctx, use_exhentai=use_exhentai)

    return app


mcp = create_server()


def main():
    transport = os.environ.get("MEDIA_HUNTER_TRANSPORT", "stdio")
    if transport not in {"stdio", "http", "sse"}:
        raise ValueError("MEDIA_HUNTER_TRANSPORT 仅支持 stdio/http/sse")
    if transport == "stdio":
        mcp.run(show_banner=False)
    else:
        mcp.run(
            transport=transport,
            host=os.environ.get("MEDIA_HUNTER_HOST", "127.0.0.1"),
            port=int(os.environ.get("MEDIA_HUNTER_PORT", "8787")),
            show_banner=False,
        )


if __name__ == "__main__":
    main()
