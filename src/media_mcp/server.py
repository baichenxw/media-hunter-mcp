"""FastMCP 入口。导入模块不加载凭证，生命周期退出时关闭 HTTP 连接。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from .config import Config
from .service import MediaService


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

    app = FastMCP("media-hunter-mcp", version="0.2.0", lifespan=lifespan)

    @app.tool
    async def search(
        site: str,
        query: str,
        limit: int = 20,
        page: int = 1,
        min_score: float | None = None,
        rating: str | None = None,
    ) -> dict:
        """搜索 e621/rule34/ehentai/pixiv。query 使用站点原生标签或关键词。
        page 从 1 开始；limit 上限分别为 320/1000/100/30。
        rating：e621 s/q/e；rule34 safe/questionable/explicit；
        pixiv all（不限）/safe/r18/r18g（精确等级）；ehentai 为画廊分类。
        min_score 为站点分数（Pixiv 收藏数、E-Hentai 星级）。过滤后可能少于 limit。
        """
        return await get_service().execute(
            "search",
            site=site,
            query=query,
            limit=limit,
            page=page,
            min_score=min_score,
            rating=rating,
        )

    @app.tool
    async def get_post(site: str, post_id: str) -> dict:
        """作品完整元数据。post_id 为数字；ehentai 为 gid/token。"""
        return await get_service().execute("get_post", site=site, post_id=post_id)

    @app.tool
    async def download_post(
        site: str, post_id: str, subdir: str | None = None, timeout: float | None = None
    ) -> dict:
        """按 ID 下载整部作品，返回文件清单及 sidecar。subdir 为下载根目录下的分组名。
        timeout 为覆盖等待、元数据、解析、下载及合成的总秒数。失败文件不会成为最终文件。
        """
        return await get_service().execute(
            "download_post", site=site, post_id=post_id, subdir=subdir, timeout=timeout
        )

    @app.tool
    async def download_search(
        site: str,
        query: str,
        limit: int = 10,
        min_score: float | None = None,
        rating: str | None = None,
        subdir: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        """搜索并批量下载，最多尝试 50 个文件。超额画廊整本跳过，可用 download_post 单独下载。
        返回 downloaded/errors/skipped；timeout 覆盖搜索和下载全流程。部分完成时 success=false，成功文件仍保留。
        """
        return await get_service().execute(
            "download_search",
            site=site,
            query=query,
            limit=limit,
            min_score=min_score,
            rating=rating,
            subdir=subdir,
            timeout=timeout,
        )

    @app.tool
    async def download_url(
        url: str, subdir: str | None = None, timeout: float | None = None
    ) -> dict:
        """识别四个站点的作品页面 URL 并下载。timeout 为总秒数；不接受任意文件直链。"""
        return await get_service().execute("download_url", url=url, subdir=subdir, timeout=timeout)

    @app.tool
    async def self_check() -> dict:
        """并行检查四站凭证及 API 连通性，每站最多 45 秒；不下载媒体、不返回凭证。"""
        return await get_service().execute("self_check")

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
