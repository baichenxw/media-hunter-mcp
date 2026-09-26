"""仅从可信预览源读取图片，在内存中生成有大小上限的 JPEG。"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .models import Post, SiteNetworkError, ValidationError
from .network import Network, check_response

MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_PIXELS = 20_000_000
MAX_PREVIEW_EDGE = 640
MAX_PREVIEW_BYTES = 96 * 1024
MAX_REDIRECTS = 3
READ_CHUNK_BYTES = 64 * 1024
# 请求取消不能中止 Pillow 的原生解码；独立线程池限制真正运行的任务数。
# 线程在首次提交时创建，解释器退出时会等待有界的图片处理完成。
_COMPRESSION_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-preview")
_REDIRECT_STATUS = {301, 302, 303, 307, 308}
_INPUT_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")
_CONTENT_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
    "application/octet-stream",
    "binary/octet-stream",
}


@dataclass(frozen=True)
class PreviewImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    source_url: str
    source_kind: str
    animated_first_frame: bool

    def metadata(self) -> dict:
        """图片内容另用 MCP ImageContent 传输，避免在文本中重复 Base64。"""
        return {
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "size_bytes": len(self.data),
            "source_kind": self.source_kind,
            "animated_first_frame": self.animated_first_frame,
        }


def _https_parts(url: str):
    if (
        not isinstance(url, str)
        or len(url) > 4096
        or any(ord(c) <= 32 or ord(c) == 127 for c in url)
    ):
        raise ValidationError("预览图片地址格式无效")
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.hostname
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
            and not parts.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValidationError("预览仅支持无凭证的 HTTPS 图片地址及默认端口")
    return parts


def _official_host(site: str, host: str, path: str) -> bool:
    if site == "e621":
        return bool(re.fullmatch(r"static\d+\.e621\.net", host))
    if site == "rule34":
        return host == "rule34.xxx" or host.endswith(".rule34.xxx")
    if site == "ehentai":
        return (
            host == "ehgt.org"
            or host.endswith(".ehgt.org")
            or (host in {"exhentai.org", "s.exhentai.org"} and path.startswith("/t/"))
        )
    if site == "pixiv":
        return host in {"i.pximg.net", "i-f.pximg.net"}
    return False


def _mirror_parts(image_mirror: str | None):
    if not image_mirror:
        return None
    parts = _https_parts(image_mirror)
    host = parts.hostname
    if parts.query:
        raise ValidationError("预览图片镜像基地址不能带查询参数")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        public = (
            "." in host
            and host != "localhost"
            and not host.endswith((".localhost", ".local", ".internal", ".lan", ".home"))
        )
    else:
        public = address.is_global
    if not public:
        raise ValidationError("预览图片镜像不能指向本机或私网地址")
    return parts


def _validate_source(site: str, url: str, mirror=None) -> str:
    parts = _https_parts(url)
    if _official_host(site, parts.hostname, parts.path):
        return url
    if (
        site == "pixiv"
        and mirror is not None
        and parts.hostname == mirror.hostname
        and parts.path.startswith(mirror.path.rstrip("/") + "/")
    ):
        return url
    raise ValidationError(
        "预览图片来源不在该站点允许的图片域名中",
        "仅支持站点提供的缩略图；Pixiv 可使用配置中明确指定的 HTTPS image_mirror",
    )


def _initial_url(post: Post, image_mirror: str | None):
    if not post.preview_url:
        raise ValidationError(
            "该作品没有可用的预览图", "可查看作品元数据，但不能据此声称已看过图片"
        )
    url = _validate_source(post.site, post.preview_url)
    mirror = _mirror_parts(image_mirror) if post.site == "pixiv" else None
    if mirror is not None and urlsplit(url).hostname == "i.pximg.net":
        original = urlsplit(url)
        url = urlunsplit(
            (
                mirror.scheme,
                mirror.netloc,
                mirror.path.rstrip("/") + original.path,
                original.query,
                "",
            )
        )
    return _validate_source(post.site, url, mirror), mirror


async def _read_preview(
    network: Network, post: Post, image_mirror: str | None
) -> tuple[bytes, str]:
    url, mirror = _initial_url(post, image_mirror)
    headers = {"Accept-Encoding": "identity", "Accept": "image/jpeg,image/png,image/webp,image/gif"}
    if post.site == "pixiv":
        headers["Referer"] = "https://www.pixiv.net/"
    try:
        for redirects in range(MAX_REDIRECTS + 1):
            async with network.stream(
                post.site,
                "GET",
                url,
                allow_mirror=False,
                follow_redirects=False,
                omit_cookies=True,
                headers=headers,
            ) as response:
                if response.status_code in _REDIRECT_STATUS:
                    if redirects == MAX_REDIRECTS:
                        raise SiteNetworkError("预览图片重定向次数过多")
                    location = response.headers.get("location")
                    if not location:
                        raise SiteNetworkError("预览图片重定向缺少目标地址")
                    url = _validate_source(post.site, urljoin(url, location), mirror)
                    continue
                check_response(response, post.site)
                # 不让 HTTP 解压先于体积检查，图片格式内部压缩另由像素上限约束。
                if (
                    response.headers.get("content-encoding", "identity").strip().lower()
                    != "identity"
                ):
                    raise ValidationError("预览源忽略 identity 编码请求，无法安全限制响应体积")
                content_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                )
                if content_type and content_type not in _CONTENT_TYPES:
                    raise ValidationError(
                        "预览源没有返回支持的静态图片格式", "支持 JPEG、PNG、WebP 和 GIF 首帧"
                    )
                content_length = response.headers.get("content-length", "")
                if content_length.isdecimal() and int(content_length) > MAX_SOURCE_BYTES:
                    raise ValidationError("预览源图片超过 8 MiB 上限")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=READ_CHUNK_BYTES):
                    if len(data) + len(chunk) > MAX_SOURCE_BYTES:
                        raise ValidationError("预览源图片超过 8 MiB 上限")
                    data.extend(chunk)
                return bytes(data), url
    except httpx.HTTPError:
        raise SiteNetworkError("读取预览图片时网络连接中断", "可稍后重试该作品的预览") from None
    raise SiteNetworkError("未能获取预览图片")


def _compress_preview(data: bytes, post: Post, source_url: str) -> PreviewImage:
    try:
        with Image.open(BytesIO(data), formats=_INPUT_FORMATS) as source:
            if source.width * source.height > MAX_SOURCE_PIXELS:
                raise ValidationError("预览源图片超过 2000 万像素上限")
            animated = bool(getattr(source, "is_animated", False))
            source.seek(0)
            # 先缩小再做 EXIF 方向修正，避免为大源图额外复制完整像素缓冲区。
            source.thumbnail((MAX_PREVIEW_EDGE, MAX_PREVIEW_EDGE), Image.Resampling.LANCZOS)
            oriented = ImageOps.exif_transpose(source)
            rgba = oriented.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            rgb.info.clear()
        while True:
            for quality in (82, 70, 58, 46, 34, 24):
                output = BytesIO()
                rgb.save(
                    output,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                    exif=b"",
                    icc_profile=None,
                )
                result = output.getvalue()
                if len(result) <= MAX_PREVIEW_BYTES:
                    return PreviewImage(
                        data=result,
                        mime_type="image/jpeg",
                        width=rgb.width,
                        height=rgb.height,
                        source_url=source_url,
                        source_kind="gallery_cover"
                        if post.site == "ehentai"
                        else "artwork_thumbnail",
                        animated_first_frame=animated,
                    )
            rgb.thumbnail(
                (max(1, rgb.width * 3 // 4), max(1, rgb.height * 3 // 4)),
                Image.Resampling.LANCZOS,
            )
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValidationError("预览源图片像素过大，已拒绝解码") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise ValidationError(
            "预览源不是完整可解码的图片", "支持 JPEG、PNG、WebP 和 GIF 首帧；不支持 SVG/HTML/视频"
        ) from None


async def preview_post(
    network: Network, post: Post, *, image_mirror: str | None = None
) -> PreviewImage:
    """返回一张内存 JPEG；E 站仅画廊封面，多页/动画均不代表完整内容。"""
    data, source_url = await _read_preview(network, post, image_mirror)
    future = _COMPRESSION_EXECUTOR.submit(_compress_preview, data, post, source_url)
    try:
        return await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        # 尚未开始的工作不再执行；已开始的工作仍占用自己的线程池槽位。
        future.cancel()
        raise
