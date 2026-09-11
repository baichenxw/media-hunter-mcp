"""统一数据模型与错误类型。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum

import httpx


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    GALLERY = "gallery"
    UGOIRA = "ugoira"


@dataclass
class Post:
    """四个站点归一化后的作品/画廊描述。"""

    site: str
    id: str
    url: str
    title: str = ""
    tags: list[str] = field(default_factory=list)
    artist: list[str] = field(default_factory=list)
    rating: str = ""
    score: float | None = None
    media_type: MediaType = MediaType.IMAGE
    file_url: str | None = None
    preview_url: str | None = None
    page_count: int = 1
    extra: dict = field(default_factory=dict)
    # 仅在一次业务操作内复用站点详情，不进入工具输出或 sidecar。
    _download_data: dict = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("_download_data")
        data["media_type"] = self.media_type.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Post:
        data = dict(data)
        data["media_type"] = MediaType(data.get("media_type", "image"))
        return cls(**data)


@dataclass
class DownloadTarget:
    """一个待下载文件。post_process="ugoira" 时下载完成后调用合成流程。"""

    url: str
    filename: str
    page: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    post_process: str | None = None
    post_process_meta: dict = field(default_factory=dict)
    # 仅在进程内使用；画廊在真正下载前才解析短期有效的图片 URL。
    resolve: Callable[[DownloadTarget], Awaitable[DownloadTarget]] | None = field(
        default=None, repr=False, compare=False
    )
    # 在同一次流式 GET 中校验响应、确定文件名，避免原图入口被重复请求。
    response_filename: Callable[[httpx.Response], Awaitable[str]] | None = field(
        default=None, repr=False, compare=False
    )


@dataclass
class DownloadedFile:
    path: str
    page: int
    size: int
    sha256: str = ""
    source_key: str = ""
    reused: bool = False


@dataclass
class DownloadResult:
    post: Post
    directory: str
    files: list[DownloadedFile]
    sidecar_path: str
    errors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "post": self.post.to_dict(),
            "directory": self.directory,
            "files": [asdict(f) for f in self.files],
            "sidecar_path": self.sidecar_path,
            "errors": self.errors,
            "complete": not self.errors,
            "reused_files": sum(f.reused for f in self.files),
            "new_files": sum(not f.reused for f in self.files),
        }


class MediaMcpError(Exception):
    """工具层统一错误基类：error_type / message / hint。"""

    error_type = "internal"

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict:
        return {"type": self.error_type, "message": self.message, "hint": self.hint}


class AuthError(MediaMcpError):
    error_type = "auth"


class ValidationError(MediaMcpError):
    error_type = "validation"


class QuotaError(MediaMcpError):
    error_type = "quota"


class NotFoundError(MediaMcpError):
    error_type = "not_found"


class SiteNetworkError(MediaMcpError):
    error_type = "network"


class ToolTimeoutError(MediaMcpError):
    error_type = "timeout"
