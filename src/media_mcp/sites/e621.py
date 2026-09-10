"""e621 适配器：官方 JSON API（/posts.json）。"""

from __future__ import annotations

import re

from ..models import (
    AuthError,
    DownloadTarget,
    MediaType,
    NotFoundError,
    Post,
    SiteNetworkError,
)
from ..network import response_json
from .base import SiteAdapter

VIDEO_EXTS = {"webm", "mp4"}
DEFAULT_UA = "media-hunter-mcp/0.1 (by local user)"


class E621Adapter(SiteAdapter):
    name = "e621"
    BASE = "https://e621.net"

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": str(self._config.site_get("e621", "user_agent", DEFAULT_UA))}

    def _auth(self) -> tuple[str, str] | None:
        username = str(self._config.site_get("e621", "username", "") or "")
        api_key = str(self._config.site_get("e621", "api_key", "") or "")
        return (username, api_key) if username and api_key else None

    async def _get_posts(self, params: dict) -> list[dict]:
        response = await self._network.request(
            self.name,
            "GET",
            f"{self.BASE}/posts.json",
            headers=self._headers(),
            params=params,
            auth=self._auth(),
        )
        if response.status_code in (401, 403):
            raise AuthError(
                "e621 拒绝了请求",
                hint="检查 [sites.e621] 的 username/api_key；User-Agent 不要伪装浏览器",
            )
        if response.status_code != 200:
            raise SiteNetworkError(f"e621 返回 HTTP {response.status_code}")
        data = response_json(response, self.name)
        if not isinstance(data, dict) or not isinstance(data.get("posts"), list):
            raise SiteNetworkError("e621 响应缺少 posts 列表")
        return data["posts"]

    async def search(self, query, limit=20, page=1, min_score=None, rating=None):
        tags = query.strip()
        if rating:
            tags += f" rating:{rating}"
        if min_score is not None:
            tags += f" score:>={min_score}"
        posts = await self._get_posts({"tags": tags.strip(), "limit": limit, "page": page})
        return [self._to_post(p) for p in posts]

    async def get_post(self, post_id: str) -> Post:
        posts = await self._get_posts({"tags": f"id:{post_id}", "limit": 1})
        if not posts:
            raise NotFoundError(f"e621 找不到作品 {post_id}")
        return self._to_post(posts[0])

    async def get_download_targets(self, post: Post) -> list[DownloadTarget]:
        if not post.file_url:
            raise NotFoundError(f"e621 作品 {post.id} 无可用文件（可能被删除或受限）")
        artist = post.artist[0] if post.artist else "unknown"
        md5 = str(post.extra.get("md5", ""))[:8]
        ext = str(post.extra.get("ext", "jpg"))
        return [
            DownloadTarget(
                url=post.file_url,
                filename=f"{post.id}_{artist}_{md5}.{ext}",
                headers=self._headers(),
            )
        ]

    def parse_url(self, url: str) -> str | None:
        parts = self._url_parts(url, {"e621.net", "www.e621.net"})
        match = re.fullmatch(r"/(?:posts|post/show)/(\d+)/?", parts.path) if parts else None
        return match.group(1) if match else None

    async def check(self) -> dict:
        try:
            await self._get_posts({"limit": 1})
        except Exception as exc:  # noqa: BLE001 自检不抛异常
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "detail": "posts.json 可访问"}

    @staticmethod
    def _to_post(raw: dict) -> Post:
        tags = raw.get("tags", {})
        flat = [tag for group in tags.values() for tag in group]
        file_info = raw.get("file", {})
        ext = file_info.get("ext", "")
        return Post(
            site="e621",
            id=str(raw["id"]),
            url=f"{E621Adapter.BASE}/posts/{raw['id']}",
            tags=flat,
            artist=list(tags.get("artist", [])),
            rating=raw.get("rating", ""),
            score=raw.get("score", {}).get("total"),
            media_type=MediaType.VIDEO if ext in VIDEO_EXTS else MediaType.IMAGE,
            file_url=file_info.get("url"),
            preview_url=raw.get("preview", {}).get("url"),
            extra={"md5": file_info.get("md5", ""), "ext": ext},
        )
