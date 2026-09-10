"""Pixiv App API：异步 OAuth、令牌续期、插画/多图/ugoira。"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from ..models import (
    AuthError,
    DownloadTarget,
    MediaType,
    NotFoundError,
    Post,
    QuotaError,
    SiteNetworkError,
)
from ..network import _swap_base, response_json
from .base import SiteAdapter

# Pixiv 官方客户端的公开 OAuth 标识，与 pixivpy 的协议保持一致。
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
HASH_SECRET = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"
REFERER = "https://pixiv.net"


class PixivAdapter(SiteAdapter):
    name = "pixiv"

    def __init__(self, config, network):
        super().__init__(config, network)
        self._token = ""
        self._refresh_token = str(config.site_get(self.name, "refresh_token", "") or "")
        self._expires_at = 0.0
        self._auth_lock = asyncio.Lock()

    async def _authenticate(self):
        async with self._auth_lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            if not self._refresh_token:
                raise AuthError("缺少 pixiv refresh_token", "填写 [sites.pixiv].refresh_token")
            stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            base = self._config.site_get(self.name, "oauth_base", "https://oauth.secure.pixiv.net")
            response = await self._network.request(
                self.name,
                "POST",
                base.rstrip("/") + "/auth/token",
                allow_mirror=False,
                headers={
                    "User-Agent": "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)",
                    "App-OS": "ios",
                    "App-OS-Version": "14.6",
                    "X-Client-Time": stamp,
                    "X-Client-Hash": hashlib.md5((stamp + HASH_SECRET).encode()).hexdigest(),
                },
                data={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "get_secure_url": "1",
                },
            )
            if response.status_code in (400, 401, 403):
                raise AuthError(
                    "pixiv refresh_token 无效或认证被拒绝", "更新 [sites.pixiv].refresh_token"
                )
            data = response_json(response, self.name)
            token = data.get("response", data) if isinstance(data, dict) else {}
            if not token.get("access_token"):
                raise AuthError("pixiv 认证未返回 access_token")
            self._token = token["access_token"]
            self._refresh_token = token.get("refresh_token") or self._refresh_token
            self._expires_at = time.monotonic() + max(0, int(token.get("expires_in", 3600)) - 60)
            return self._token

    async def _api(self, endpoint, **params):
        for attempt in range(2):
            token = await self._authenticate()
            base = self._config.site_get(self.name, "api_base", "https://app-api.pixiv.net")
            response = await self._network.request(
                self.name,
                "GET",
                base.rstrip("/") + endpoint,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)",
                },
                params=params,
            )
            data = None
            try:
                data = response.json()
            except ValueError:
                pass
            error = data.get("error") if isinstance(data, dict) else None
            error_text = str(error).lower() if error else ""
            invalid_token = response.status_code == 401 or any(
                s in error_text for s in ("invalid_grant", "oauth", "access token")
            )
            if invalid_token:
                if attempt == 0:
                    async with self._auth_lock:
                        if self._token == token:
                            self._expires_at = 0
                    continue
                raise AuthError("pixiv 令牌刷新后仍被拒绝")
            data = response_json(response, self.name)
            if error:
                if "rate limit" in error_text:
                    raise QuotaError("pixiv 请求被限流", "稍后再试")
                if "not found" in error_text or "deleted" in error_text:
                    raise NotFoundError("pixiv 作品不存在或已删除")
                raise SiteNetworkError("pixiv API 返回错误", "检查作品是否可访问或稍后重试")
            if not isinstance(data, dict):
                raise SiteNetworkError("pixiv API 响应结构异常")
            return data
        raise AuthError("pixiv 认证失败")

    async def search(self, query, limit=20, page=1, min_score=None, rating=None):
        result = await self._api(
            "/v1/search/illust",
            word=query,
            sort="date_desc",
            search_target="partial_match_for_tags",
            offset=(page - 1) * 30,
        )
        if not isinstance(result.get("illusts"), list):
            raise SiteNetworkError("pixiv 搜索响应缺少 illusts")
        posts = [self._to_post(i) for i in result["illusts"]]
        if rating and rating.lower() != "all":
            level = {"safe": 0, "r18": 1, "r18g": 2}[rating.lower()]
            posts = [p for p in posts if p.extra["x_restrict"] == level]
        if min_score is not None:
            posts = [p for p in posts if (p.score or 0) >= min_score]
        return posts[: min(limit, 30)]

    async def _detail(self, post_id):
        result = await self._api("/v1/illust/detail", illust_id=post_id)
        illust = result.get("illust")
        if not isinstance(illust, dict) or not illust.get("id"):
            raise NotFoundError(f"pixiv 找不到作品 {post_id}")
        return illust

    async def get_post(self, post_id):
        return self._to_post(await self._detail(post_id))

    @staticmethod
    def _to_post(illust):
        user = illust.get("user") or {}
        kind = illust.get("type", "illust")
        count = int(illust.get("page_count") or 1)
        restrict = int(illust.get("x_restrict") or 0)
        return Post(
            site="pixiv",
            id=str(illust["id"]),
            url=f"https://www.pixiv.net/artworks/{illust['id']}",
            title=illust.get("title") or "",
            tags=[t["name"] for t in illust.get("tags", [])],
            artist=[user["name"]] if user.get("name") else [],
            rating={0: "all", 1: "R-18", 2: "R-18G"}.get(restrict, "all"),
            score=int(illust.get("total_bookmarks") or 0),
            media_type=MediaType.UGOIRA
            if kind == "ugoira"
            else MediaType.GALLERY
            if count > 1
            else MediaType.IMAGE,
            page_count=count,
            preview_url=(illust.get("image_urls") or {}).get("medium"),
            extra={"x_restrict": restrict, "user_id": user.get("id", 0), "illust_type": kind},
        )

    def _rewrite_image_url(self, url):
        mirror = self._config.site_get(self.name, "image_mirror", "")
        return (
            _swap_base(url, mirror) if mirror and urlsplit(url).hostname == "i.pximg.net" else url
        )

    async def get_download_targets(self, post):
        if post.media_type == MediaType.UGOIRA:
            data = await self._api("/v1/ugoira/metadata", illust_id=post.id)
            meta = data.get("ugoira_metadata") or {}
            urls = meta.get("zip_urls") or {}
            url = urls.get("original") or urls.get("medium")
            if not url:
                raise NotFoundError("pixiv 动图无 ZIP 下载地址")
            return [
                DownloadTarget(
                    self._rewrite_image_url(url),
                    f"{post.id}_ugoira.zip",
                    headers={"Referer": REFERER},
                    post_process="ugoira",
                    post_process_meta={
                        "frames": meta.get("frames", []),
                        "delays": [f["delay"] for f in meta.get("frames", [])],
                    },
                )
            ]
        illust = await self._detail(post.id)
        pages = illust.get("meta_pages") or []
        urls = (
            [p["image_urls"]["original"] for p in pages]
            if pages
            else [(illust.get("meta_single_page") or {}).get("original_image_url")]
        )
        if not all(urls):
            raise NotFoundError("pixiv 作品无原图下载地址")
        return [
            DownloadTarget(
                self._rewrite_image_url(url),
                f"{post.id}_p{i}.{urlsplit(url).path.rsplit('.', 1)[-1]}",
                page=i,
                headers={"Referer": REFERER},
            )
            for i, url in enumerate(urls)
        ]

    def parse_url(self, url):
        parts = self._url_parts(url, {"pixiv.net", "www.pixiv.net"})
        if parts is None:
            return None
        match = re.fullmatch(r"/(?:[a-z]{2}/)?artworks/(\d+)/?", parts.path)
        if match:
            return match[1]
        value = parse_qs(parts.query).get("illust_id", [""])[0]
        return value if parts.path == "/member_illust.php" and value.isdigit() else None

    async def check(self):
        try:
            await self._api(
                "/v1/search/illust", word="landscape", search_target="partial_match_for_tags"
            )
            return {"ok": True, "detail": "OAuth 与搜索 API 可访问"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}
