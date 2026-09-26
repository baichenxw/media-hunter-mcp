"""FastMCP 入口。导入模块不加载凭证，生命周期退出时关闭 HTTP 连接。"""

from __future__ import annotations

import base64
import json
import os
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from mcp.types import ImageContent, TextContent
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
Site = Annotated[
    Literal["e621", "rule34", "ehentai", "pixiv"],
    Field(
        description="目标站点。表站 E-Hentai 和里站 ExHentai 都填 ehentai，用 use_exhentai 区分。"
    ),
]
PostId = Annotated[
    str,
    Field(
        min_length=1,
        description="作品 ID 字符串，优先从搜索结果 posts[].id 原样复制。E 站为 gid/token（如 123/abc123），其他站为数字字符串（如 123）。不是页面 URL。",
    ),
]
Subdir = Annotated[
    str | None,
    Field(
        description="下载根目录下的可选分组名（例如 landscape）；不是任意绝对路径。省略使用默认站点目录。",
    ),
]
PositiveTimeout = Annotated[
    float | None,
    Field(
        gt=0,
        allow_inf_nan=False,
        description="本次操作的总超时秒数，含排队、详情请求、下载和合成；省略或 null 不限制总时长。",
    ),
]
Overwrite = Annotated[
    bool,
    Field(
        description="false 校验并复用已下载文件；true 强制重新下载，成功后替换已有文件。",
    ),
]
EHChoice = Annotated[
    bool | None,
    Field(
        description="仅 E 站：true 里站（需要有效 Cookie），false 表站；省略/null 沿用配置（默认里站）。接续搜索结果时复制 posts[].extra.use_exhentai；其他站省略。",
    ),
]
EHUrlChoice = Annotated[
    bool | None,
    Field(
        description="仅 E 站：true 强制里站，false 强制表站；省略/null 按 URL 域名选择。其他站省略。",
    ),
]
Original = Annotated[
    bool,
    Field(
        description="仅 E 站可设 true：请求原图，可能消耗 FIQ/GP，须配置 allow_original=true。false 使用页面图。原图单独保存，失败不退回缩放图。其他站保持 false。",
    ),
]

POST_SCHEMA = {
    "type": "object",
    "description": "作品元数据；提供 ID 和页面链接，不代表文件已下载。",
    "properties": {
        "site": {"type": "string", "description": "站点标识"},
        "id": {"type": "string", "description": "放入预览或下载工具的 post_ids 数组"},
        "url": {"type": "string", "description": "作品页面 URL，可传给 media_hunter_download_url"},
        "title": {"type": "string"},
        "preview_url": {
            "type": ["string", "null"],
            "description": "缩略图地址，不是图片内容；实际看图请用 media_hunter_preview",
        },
        "page_count": {"type": "integer", "description": "整部作品的页数/文件数估计"},
        "extra": {"type": "object", "description": "站点额外信息；E 站 use_exhentai 表示本次选择"},
    },
    "required": ["site", "id", "url"],
}
DOWNLOAD_DATA_SCHEMA = {
    "type": "object",
    "properties": {
        "post": POST_SCHEMA,
        "directory": {"type": "string", "description": "服务器本地输出目录"},
        "files": {
            "type": "array",
            "description": "已完成/复用文件（不是公共下载链接）",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "服务器本地文件路径"},
                    "page": {"type": "integer"},
                    "size": {"type": "integer"},
                    "reused": {"type": "boolean"},
                },
            },
        },
        "sidecar_path": {"type": "string", "description": "元数据及下载清单 JSON 的本地路径"},
        "errors": {"type": "array", "items": {"type": "object"}},
        "complete": {"type": "boolean", "description": "是否全部完成"},
        "new_files": {"type": "integer"},
        "reused_files": {"type": "integer"},
    },
    "required": ["post", "directory", "files", "sidecar_path", "errors", "complete"],
}


def result_schema(data_schema):
    """按工具描述 data 的结构，同时允许仅含 error 的失败结果。"""
    return {**OUTPUT_SCHEMA, "properties": {**OUTPUT_SCHEMA["properties"], "data": data_schema}}


SEARCH_OUTPUT = result_schema(
    {
        "type": "object",
        "properties": {
            "posts": {"type": "array", "items": POST_SCHEMA},
            "count": {"type": "integer", "description": "本页过滤后的结果数量，不是站点总数"},
            "page": {"type": "integer"},
            "limit": {"type": "integer"},
        },
        "required": ["posts", "count", "page", "limit"],
    }
)
POST_OUTPUT = result_schema(POST_SCHEMA)
PREVIEW_OUTPUT = result_schema(
    {
        "type": "object",
        "properties": {
            "requested_ids": {"type": "array", "items": {"type": "string"}},
            "previews": {
                "type": "array",
                "description": "成功返回的图片，顺序与 content 中的 image 块一一对应；失败项没有图片",
                "items": {
                    "type": "object",
                    "properties": {
                        "site": {"type": "string"},
                        "id": {"type": "string"},
                        "image_index": {"type": "integer", "description": "第几张图片，从 1 开始"},
                        "mime_type": {"type": "string", "const": "image/jpeg"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "size_bytes": {
                            "type": "integer",
                            "description": "JPEG 字节数，不含 Base64 开销",
                        },
                        "scope": {"type": "string", "enum": ["cover", "thumbnail"]},
                        "page_count": {
                            "type": "integer",
                            "description": "作品页数；本次只看到了封面或缩略图",
                        },
                        "use_exhentai": {"type": ["boolean", "null"]},
                    },
                    "required": [
                        "site",
                        "id",
                        "image_index",
                        "mime_type",
                        "width",
                        "height",
                        "size_bytes",
                        "scope",
                    ],
                },
            },
            "count": {"type": "integer"},
            "total_bytes": {"type": "integer"},
            "errors": {"type": "array", "items": {"type": "object"}},
            "skipped": {"type": "array", "items": {"type": "object"}},
            "complete": {"type": "boolean"},
        },
        "required": [
            "requested_ids",
            "previews",
            "count",
            "total_bytes",
            "errors",
            "skipped",
            "complete",
        ],
    }
)
DOWNLOAD_OUTPUT = result_schema(DOWNLOAD_DATA_SCHEMA)
SELECTED_DOWNLOAD_OUTPUT = result_schema(
    {
        "type": "object",
        "properties": {
            "requested_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "按输入顺序去重后的选定 ID",
            },
            "downloaded": {
                "type": "array",
                "items": {
                    **DOWNLOAD_DATA_SCHEMA,
                    "properties": {"id": {"type": "string"}, **DOWNLOAD_DATA_SCHEMA["properties"]},
                    "required": ["id", *DOWNLOAD_DATA_SCHEMA["required"]],
                },
                "description": "已处理作品，不等于全部成功；逐项读取 complete/errors，可能仅部分文件完成",
            },
            "errors": {
                "type": "array",
                "items": {"type": "object"},
                "description": "带作品 id 的失败原因",
            },
            "skipped": {
                "type": "array",
                "items": {"type": "object"},
                "description": "本次未处理的 id 及 reason；不表示服务器上没有以前下载的文件",
            },
            "total_files": {
                "type": "integer",
                "description": "本次已返回结果的新下载与复用文件数，不是磁盘文件总量",
            },
            "attempted_files": {
                "type": "integer",
                "description": "已进入处理的文件目标数，包含复用及失败目标；去重后多个 ID 时最多 50，单 ID 不限",
            },
            "new_files": {"type": "integer"},
            "reused_files": {"type": "integer"},
            "stop_reason": {
                "type": ["object", "null"],
                "description": "登录/额度错误导致的停止原因，否则 null；其他错误查看 errors",
            },
            "complete": {"type": "boolean"},
        },
        "required": [
            "requested_ids",
            "downloaded",
            "errors",
            "skipped",
            "total_files",
            "attempted_files",
            "complete",
        ],
    }
)
CHECK_OUTPUT = result_schema(
    {
        "type": "object",
        "description": "检查结果按站点组织；总体 success=true 不代表每站都可用，须读取各站 ok。",
        "properties": {
            site: {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "detail": {"type": "string"},
                },
            }
            for site in ("e621", "rule34", "ehentai", "pixiv")
        },
    }
)


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
        instructions=(
            "Media Hunter：在 e621、rule34、E-Hentai/ExHentai、Pixiv 搜索、看预览和下载作品。"
            "搜索用 media_hunter_search，详情用 media_hunter_get_post，两者只返回文字元数据。"
            "需要看画面时用 media_hunter_preview，返回压缩图片，不保存文件。"
            "需要保存媒体时，将选定 ID 交给 "
            "media_hunter_download 的 post_ids 数组（单个 ID 也用数组）。"
            "已有作品页面 URL 可直接调用 media_hunter_download_url。"
            "工具结果均为 success/data/error；失败或部分完成时查看 error 和 data 中的已完成文件。"
        ),
        lifespan=lifespan,
        strict_input_validation=True,
        cache_ttl=60,
        cache_scope="private",
    )

    async def call(operation, ctx, **kwargs):
        result = await get_service().execute(operation, progress=ctx.report_progress, **kwargs)
        # 保留 JSON 文本及结构化清单，包括部分失败的成功文件。
        return ToolResult(structured_content=result, is_error=not result["success"])

    @app.tool(
        title="Media Hunter · 搜索作品（只读）",
        annotations=READ_ANNOTATIONS,
        output_schema=SEARCH_OUTPUT,
    )
    async def media_hunter_search(
        site: Site,
        query: Annotated[
            str,
            Field(
                min_length=1,
                pattern=r"\S",
                description="站点原生标签或关键词。e621/rule34 使用空格分隔标签，如 landscape；Pixiv/E 站用标题或标签关键词，如 風景。",
            ),
        ],
        ctx: Context,
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=1000,
                description="单页请求条数；e621≤320，rule34≤1000，ehentai≤100，pixiv≤30。过滤后可能更少。不是下载数量。",
            ),
        ] = 20,
        page: Annotated[
            int,
            Field(ge=1, description="搜索页码，从 1 开始；E 站最多 100 页，需逐页遍历，深页较慢。"),
        ] = 1,
        min_score: Annotated[
            float | None,
            Field(
                allow_inf_nan=False,
                description="最低站点评分；Pixiv 指收藏数，E 站指星级。省略不限。",
            ),
        ] = None,
        rating: Annotated[
            str | None,
            Field(
                description="e621：s/q/e 或 safe/questionable/explicit；rule34：safe/questionable/explicit；Pixiv：all/safe/r18/r18g；E 站：分类名，如 Manga、Non-H。省略不限。"
            ),
        ] = None,
        use_exhentai: EHChoice = None,
    ) -> ToolResult:
        """按关键词发现作品，只读取搜索与元数据，不下载媒体、不创建下载目录。
        适合用户尚未提供作品 ID/链接时调用。返回 data.posts（含 site/id/url/title/page_count）、count/page/limit。
        count=0 表示当前页无匹配结果，可调整关键词、过滤条件或页码。
        标题、标签和 preview_url 只是文字，不能据此声称看过画面；需要视觉筛选时，用 media_hunter_preview 看选定 ID 的图片。
        需要保存媒体时，把选定的 posts[].id 放入 media_hunter_download 的 post_ids 数组，单个或多个均可。
        E 站接续按 ID 下载时，沿用结果 extra.use_exhentai；直接用结果 url 下载也可保持站点选择。
        示例：{"site":"pixiv","query":"風景","rating":"safe","limit":5}。
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

    @app.tool(
        title="Media Hunter · 获取作品详情（只读）",
        annotations=READ_ANNOTATIONS,
        output_schema=POST_OUTPUT,
    )
    async def media_hunter_get_post(
        site: Site, post_id: PostId, ctx: Context, use_exhentai: EHChoice = None
    ) -> ToolResult:
        """读取已知 ID 的作品详情，不搜索、不下载媒体、不创建下载目录。
        用于下载前查看标题、标签、评分、页数；返回 data 中的作品元数据，ID 不存在时返回 not_found 错误。
        不返回图片内容，不能用来判断构图、颜色等视觉特征；实际看图用 media_hunter_preview，post_ids=[该 ID]。
        ID 来自 media_hunter_search 的 posts[].id；E 站 ID 为 gid/token，其他站为数字字符串。
        要保存文件请调用 media_hunter_download，post_ids=[该作品 ID]；已有页面链接可直接用 media_hunter_download_url。
        """
        return await call("get_post", ctx, site=site, post_id=post_id, use_exhentai=use_exhentai)

    @app.tool(
        title="Media Hunter · 查看作品预览图（只读）",
        annotations=READ_ANNOTATIONS,
        output_schema=PREVIEW_OUTPUT,
    )
    async def media_hunter_preview(
        site: Site,
        post_ids: Annotated[
            list[PostId],
            Field(
                min_length=1,
                max_length=4,
                description='同一站点要看的 1–4 个作品 ID 字符串，单个也传数组，如 ["123"]；按顺序去重。不是关键词或图片 URL；更多作品分批预览。',
            ),
        ],
        ctx: Context,
        use_exhentai: EHChoice = None,
        timeout: Annotated[
            float | None,
            Field(
                gt=0,
                allow_inf_nan=False,
                description="整批预览的总超时秒数，含排队、详情与图片处理；默认 45 秒，null 不限总时长。超时保留已返回的预览。",
            ),
        ] = 45.0,
    ) -> ToolResult:
        """看作品的实际缩略图，用于按画面筛选后再决定是否下载；只在内存处理，不保存媒体文件。
        返回 MCP 图片内容供视觉模型直接查看，以及 data.previews 清单；搜索/详情里的 preview_url 本身不等于看过图片。
        已知 ID 直接调用；未知 ID 先 media_hunter_search。单个或多个都用 post_ids 数组，最多 4 个；不同站点或表/里站分次调用。
        每作品仅 1 张：E 站为画廊封面，其他站为站点缩略图；多页作品不能据此判断其余页，视频/动图仅静态预览。
        图片为最长边不超过 640 像素、每张不超过 96 KiB 的 JPEG，不请求原图，也不调用下载工具。无法读取预览时不自动下载原图补看。
        按 data.previews[].image_index（从 1 开始）对应返回的图片，id 用于后续 media_hunter_download；E 站沿用 use_exhentai。
        成功、失败可能并存，查看 success/data.errors/skipped；仅评价实际返回的图片。若客户端未把图片传给视觉模型，应说明未能看图，不凭标签猜测。
        示例：{"site":"pixiv","post_ids":["123","456"]}。只有用户需要保存媒体时才调用下载工具。
        """
        result, images = await get_service().preview_posts(
            site,
            post_ids,
            use_exhentai=use_exhentai,
            timeout=timeout,
            progress=ctx.report_progress,
        )
        content = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        for item, image in zip(result.get("data", {}).get("previews", []), images, strict=True):
            content.append(
                TextContent(
                    type="text",
                    text=f"预览图 {item['image_index']}：site={item['site']}，id={item['id']}，仅封面/缩略图。",
                )
            )
            content.append(
                ImageContent(
                    type="image",
                    data=base64.b64encode(image.data).decode("ascii"),
                    mime_type=image.mime_type,
                )
            )
        return ToolResult(
            content=content, structured_content=result, is_error=not result["success"]
        )

    @app.tool(
        title="Media Hunter · 按 ID 下载作品（单个或多个）",
        annotations=DOWNLOAD_ANNOTATIONS,
        output_schema=SELECTED_DOWNLOAD_OUTPUT,
    )
    async def media_hunter_download(
        site: Site,
        post_ids: Annotated[
            list[PostId],
            Field(
                min_length=1,
                max_length=50,
                description='同一站点明确选定的 1–50 个作品 ID 字符串；单个也用数组，如 ["123"]。不是关键词或 URL；E 站每项为 gid/token。先校验全部格式，再按输入顺序去重；去重后一个 ID 下载整部且不限文件数，多个 ID 共用 50 文件预算。',
            ),
        ],
        ctx: Context,
        subdir: Subdir = None,
        timeout: PositiveTimeout = None,
        overwrite: Overwrite = False,
        use_exhentai: EHChoice = None,
        original: Original = False,
    ) -> ToolResult:
        """将明确选定的一部或多部作品保存到服务器本地；不搜索、不自动添加作品。未知 ID 先用 media_hunter_search。
        已明确 site/ID 且要保存时直接调用，无需先搜索、取详情或预览；仅需要筛选或页数信息时再读详情/看预览。
        单个也必须传 post_ids 数组，例如 {"site":"pixiv","post_ids":["123"]}；多个为 ["123","456"]。
        去重后只有一个 ID：下载整部作品/画廊，不受 50 文件上限限制。
        去重后多个 ID：整批最多处理 50 个文件目标，复用及失败目标也占预算；超出剩余预算的作品整部跳过，不截取前几页。
        根据用户目标和页数自行分组；需要整本大画廊或补下预算跳过项时，用同一工具、post_ids 仅含该 ID。
        不同站点、表/里站或原图设置分次调用。E 站接续搜索沿用 extra.use_exhentai；original=true 请求原图，须允许且可能消耗 FIQ/GP，失败不降级。
        单个与多个均返回 data.requested_ids、downloaded（逐作品的 id/post/files/directory/sidecar_path/errors）、errors、skipped、total_files、stop_reason。
        files[].path 是服务器本地路径，不是公网链接，也不代表已发送给用户；交付文件需客户端的附件/发送能力，不能编造下载链接。
        检查 success 和逐项 complete；失败/跳过会返回 success=false/isError=true，已完成文件保留。
        downloaded 仅表示已处理，可能含部分失败作品；逐项检查 complete/errors。skipped 仅表示本次未处理，不能断言以前没有文件。
        登录/额度错误停止剩余作品，应先处理原因；普通作品错误继续后续项。按 errors/skipped 的 ID 决定重试，不必重新搜索。
        默认 overwrite=false 校验复用成功文件，适合补齐；overwrite=true 强制重下。timeout 含排队、详情、下载与合成，省略不限时。
        超时中断的作品可能不在 downloaded 中，已完成页记录在本地 sidecar，total_files 不计这些页；同 ID 重试会校验复用。
        total_files 只统计本次返回清单中的文件，不是服务器磁盘文件总数。
        """
        return await call(
            "download_posts",
            ctx,
            site=site,
            post_ids=post_ids,
            subdir=subdir,
            timeout=timeout,
            overwrite=overwrite,
            use_exhentai=use_exhentai,
            original=original,
        )

    @app.tool(
        title="Media Hunter · 按页面链接下载",
        annotations=DOWNLOAD_ANNOTATIONS,
        output_schema=DOWNLOAD_OUTPUT,
    )
    async def media_hunter_download_url(
        url: Annotated[
            str,
            Field(
                min_length=1,
                description="完整作品页面 URL：e621.net/posts/ID、rule34.xxx/index.php?page=post&s=view&id=ID、www.pixiv.net/artworks/ID，或 e-hentai.org/exhentai.org/g/gid/token/；须带 http(s)://。不接受搜索页或图片直链。",
            ),
        ],
        ctx: Context,
        subdir: Subdir = None,
        timeout: PositiveTimeout = None,
        overwrite: Overwrite = False,
        use_exhentai: EHUrlChoice = None,
        original: Original = False,
    ) -> ToolResult:
        """已知作品页面 URL 时直接下载整部作品，自动识别站点与 ID；无需先搜索或手动拆解链接。
        只接受四个受支持站点的作品/画廊页面，不搜索、不下载任意文件直链。
        E 站默认按链接域名选表站/里站；显式 use_exhentai 可覆盖。原图须 original=true，可能消耗 FIQ/GP。
        返回 data.files/sidecar_path 等本地路径，含复用标记；部分失败时 success=false，已完成文件仍保留。
        本地路径不是公网下载链接；交付文件需客户端的附件/发送能力。仅查看图片请用 media_hunter_preview，不要先下载整部作品。
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

    @app.tool(
        title="Media Hunter · 检查连接和凭证（只读）",
        annotations=READ_ANNOTATIONS,
        output_schema=CHECK_OUTPUT,
    )
    async def media_hunter_self_check(ctx: Context, use_exhentai: EHChoice = None) -> ToolResult:
        """诊断四个站点的账号配置与 API/首页连通性，每站最多 45 秒；不下载媒体、不返回凭证。
        配置完成后或出现登录/连接错误时使用，不必在每次搜索前重复检查。
        返回 data.<站点>.ok/detail 以及 download_root/proxy；必须逐站查看 ok，总体 success=true 仅表示检查已执行。
        use_exhentai 只控制 E 站的检查目标；连通性通过不保证所有作品或媒体链接都可下载。
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
