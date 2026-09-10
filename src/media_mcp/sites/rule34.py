"""rule34.xxx 适配器：dapi JSON API（2025 年起强制 user_id + api_key）。"""

from __future__ import annotations

from urllib.parse import parse_qs

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

VIDEO_EXTS = ("webm", "mp4")


class Rule34Adapter(SiteAdapter):
    name = "rule34"
    BASE = "https://api.rule34.xxx/index.php"

    def _credentials(self) -> dict[str, str]:
        user_id = str(self._config.site_get("rule34", "user_id", "") or "")
        api_key = str(self._config.site_get("rule34", "api_key", "") or "")
        if not user_id or not api_key:
            raise AuthError(
                "rule34.xxx API 需要 user_id 和 api_key",
                hint="注册 rule34.xxx 账号后在 Options 页面生成 API key，填入 config.toml 的 [sites.rule34]",
            )
        return {"user_id": user_id, "api_key": api_key}

    async def _fetch(self, params: dict) -> list[dict]:
        query = {
            "page": "dapi",
            "s": "post",
            "q": "index",
            "json": "1",
            **params,
            **self._credentials(),
        }
        response = await self._network.request(self.name, "GET", self.BASE, params=query)
        if response.status_code in (401, 403):
            raise AuthError("rule34 API 认证失败", hint="检查 [sites.rule34] 的 user_id/api_key")
        if response.status_code != 200:
            raise SiteNetworkError(f"rule34 返回 HTTP {response.status_code}")
        if not response.text.strip():
            return []
        data = response_json(response, self.name)
        if not isinstance(data, list):
            if isinstance(data, dict) and any(key in data for key in ("error", "message")):
                raise AuthError("rule34 API 拒绝请求", "检查 user_id/api_key 与站点账户状态")
            raise SiteNetworkError("rule34 响应不是作品列表")
        return data

    async def search(self, query, limit=20, page=1, min_score=None, rating=None):
        tags = query.strip()
        if rating:
            tags += f" rating:{rating}"
        if min_score is not None:
            tags += f" score:>={min_score}"
        raw = await self._fetch({"tags": tags.strip(), "limit": limit, "pid": page - 1})
        posts = [self._to_post(p) for p in raw]
        if rating:
            # 站点过滤可能失效，返回前再次检查等级。
            posts = [p for p in posts if p.rating.lower() == rating.lower()]
        if min_score is not None:
            posts = [p for p in posts if (p.score or 0) >= min_score]
        return posts

    async def get_post(self, post_id: str) -> Post:
        raw = await self._fetch({"id": post_id, "limit": 1})
        if not raw:
            raise NotFoundError(f"rule34 找不到作品 {post_id}")
        return self._to_post(raw[0])

    async def get_download_targets(self, post: Post) -> list[DownloadTarget]:
        if not post.file_url:
            raise NotFoundError(f"rule34 作品 {post.id} 无文件直链")
        md5 = str(post.extra.get("md5", ""))[:8]
        ext = post.file_url.rsplit(".", 1)[-1].split("?")[0] or "jpg"
        stem = f"{post.id}_{md5}" if md5 else post.id
        return [DownloadTarget(url=post.file_url, filename=f"{stem}.{ext}")]

    def parse_url(self, url: str) -> str | None:
        parts = self._url_parts(url, {"rule34.xxx", "www.rule34.xxx"})
        if not parts or parts.path not in {"/index.php", "/"}:
            return None
        params = parse_qs(parts.query)
        value = params.get("id", [""])[0]
        return (
            value
            if value.isdigit() and params.get("page") == ["post"] and params.get("s") == ["view"]
            else None
        )

    async def check(self) -> dict:
        try:
            await self._fetch({"limit": 1})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "detail": "dapi 可访问且凭证有效"}

    @staticmethod
    def _to_post(raw: dict) -> Post:
        file_url = str(raw.get("file_url", "") or "")
        ext = file_url.rsplit(".", 1)[-1].split("?")[0].lower() if file_url else ""
        return Post(
            site="rule34",
            id=str(raw.get("id", "")),
            url=f"https://rule34.xxx/index.php?page=post&s=view&id={raw.get('id', '')}",
            tags=str(raw.get("tags", "")).split(),
            artist=[],
            rating=str(raw.get("rating", "")).lower(),
            score=float(raw.get("score") or 0),
            media_type=MediaType.VIDEO if ext in VIDEO_EXTS else MediaType.IMAGE,
            file_url=file_url or None,
            preview_url=raw.get("preview_url") or None,
            extra={"md5": raw.get("md5", ""), "owner": raw.get("owner", "")},
        )
