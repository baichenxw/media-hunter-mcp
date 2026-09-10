"""E-Hentai / ExHentai 适配器：网页搜索 + api.php 元数据 + 画廊页解析下载。"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..models import (
    AuthError,
    DownloadTarget,
    MediaType,
    NotFoundError,
    Post,
    QuotaError,
    SiteNetworkError,
)
from ..network import response_json
from .base import SiteAdapter

GALLERY_RE = re.compile(r"/g/(\d+)/([0-9a-f]+)/?")
IMG_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}
CATEGORIES = {
    "misc": 1,
    "doujinshi": 2,
    "manga": 4,
    "artist cg": 8,
    "game cg": 16,
    "image set": 32,
    "cosplay": 64,
    "asian porn": 128,
    "non-h": 256,
    "western": 512,
}


class EHentaiAdapter(SiteAdapter):
    name = "ehentai"

    # ---- 基础设施 ----

    def _cookie(self) -> str:
        return str(self._config.site_get("ehentai", "cookie", "") or "")

    def _base(self) -> str:
        mirror = str(self._config.site_get("ehentai", "mirror_base", "") or "")
        if mirror:
            return mirror.rstrip("/")
        if self._config.site_get("ehentai", "use_exhentai", False) and self._cookie():
            return "https://exhentai.org"
        if self._config.site_get("ehentai", "use_exhentai", False):
            raise AuthError("ExHentai 需要 cookie", "填写 cookie 或将 use_exhentai 设为 false")
        return "https://e-hentai.org"

    def _api_url(self) -> str:
        mirror = str(self._config.site_get("ehentai", "mirror_base", "") or "")
        if mirror:
            return f"{mirror.rstrip('/')}/api.php"
        if self._base().endswith("exhentai.org"):
            return "https://exhentai.org/api.php"
        return "https://api.e-hentai.org/api.php"

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "media-hunter-mcp/0.1 (gallery metadata client)"}
        cookie = self._cookie()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _get_text(self, url: str, params: dict | None = None) -> str:
        response = await self._network.request(
            self.name, "GET", url, params=params, headers=self._headers()
        )
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("image/"):
            raise AuthError(
                "ExHentai 返回 Sad Panda（Cookie 无效或未登录）",
                hint="更新 [sites.ehentai] cookie（ipb_member_id / ipb_pass_hash / igneous）",
            )
        if response.status_code in (401, 403):
            raise AuthError("E-Hentai 拒绝访问", hint="检查 [sites.ehentai] cookie 配置")
        if response.status_code == 404:
            raise NotFoundError(f"页面不存在: {url}")
        if response.status_code != 200:
            raise SiteNetworkError(f"E-Hentai 返回 HTTP {response.status_code}: {url}")
        html = response.text
        if not html.strip():
            raise SiteNetworkError("E-Hentai 返回空白页面", "检查 cookie、网络出口或稍后重试")
        lower = html.lower()
        if "exceeded your image limits" in lower or "temporarily banned" in lower:
            raise QuotaError("E-Hentai 配额用尽或暂时限流", "等待配额恢复后重试")
        if "just a moment" in lower or "cf-chl" in lower or "cf_chl_" in lower:
            raise SiteNetworkError(
                "E-Hentai 返回 Cloudflare 验证页面", "检查当前网络出口，稍后重试"
            )
        return html

    # ---- 搜索与元数据 ----

    @staticmethod
    def _parse_search_html(html: str) -> list[list[str]]:
        """从搜索结果页提取 [gid, token] 列表，去重保序。"""
        seen: set[str] = set()
        gidlist: list[list[str]] = []
        for gid, token in GALLERY_RE.findall(html):
            if gid not in seen:
                seen.add(gid)
                gidlist.append([gid, token])
        return gidlist

    async def search(self, query, limit=20, page=1, min_score=None, rating=None):
        url = f"{self._base()}/"
        params = {"f_search": query}
        if rating:
            category = CATEGORIES.get(rating.lower())
            if category is not None:
                params["f_cats"] = 1023 ^ category
        visited = set()
        for index in range(page):
            html = await self._get_text(url, params=params)
            if index + 1 == page:
                break
            soup = BeautifulSoup(html, "html.parser")
            link = soup.find("a", id="dnext") or soup.select_one('a[rel="next"]')
            if not link or not link.get("href"):
                return []
            next_url = urljoin(url, link["href"])
            if urlsplit(next_url).netloc != urlsplit(self._base()).netloc or next_url in visited:
                raise SiteNetworkError("E-Hentai 搜索分页链接异常")
            visited.add(next_url)
            url, params = next_url, None
        gidlist = self._parse_search_html(html)[:limit]
        if not gidlist:
            return []
        posts = await self._gdata(gidlist)
        if min_score is not None:
            posts = [p for p in posts if (p.score or 0) >= min_score]
        if rating:
            posts = [p for p in posts if rating.lower() in p.rating.lower()]
        return posts

    async def _gdata(self, gidlist: list[list[str]]) -> list[Post]:
        """官方 gdata API 批量取元数据，25 条/请求，连发间歇 5 秒。"""
        posts: list[Post] = []
        for i in range(0, len(gidlist), 25):
            chunk = gidlist[i : i + 25]
            response = await self._network.request(
                self.name,
                "POST",
                self._api_url(),
                json={
                    "method": "gdata",
                    "gidlist": [[int(g), t] for g, t in chunk],
                    "namespace": 1,
                },
                headers=self._headers(),
            )
            if response.status_code != 200:
                raise SiteNetworkError(f"E-Hentai API 返回 HTTP {response.status_code}")
            data = response_json(response, self.name)
            if not isinstance(data, dict) or not isinstance(data.get("gmetadata"), list):
                raise SiteNetworkError("E-Hentai API 响应缺少 gmetadata")
            for meta in data["gmetadata"]:
                if "error" not in meta:
                    posts.append(self._meta_to_post(meta))
            if i + 25 < len(gidlist):
                await asyncio.sleep(5)
        return posts

    def _meta_to_post(self, meta: dict) -> Post:
        gid = str(meta.get("gid", ""))
        token = str(meta.get("token", ""))
        try:
            score = float(meta.get("rating", 0) or 0)
        except (TypeError, ValueError):
            score = None
        return Post(
            site=self.name,
            id=f"{gid}/{token}",
            url=f"{self._base()}/g/{gid}/{token}/",
            title=str(meta.get("title", "")),
            tags=list(meta.get("tags", [])),
            artist=[t.split(":", 1)[1] for t in meta.get("tags", []) if t.startswith("artist:")],
            rating=str(meta.get("category", "")),
            score=score,
            media_type=MediaType.GALLERY,
            preview_url=str(meta.get("thumb", "")) or None,
            page_count=int(meta.get("filecount", 0) or 0),
            extra={"gid": gid, "token": token, "posted": meta.get("posted", "")},
        )

    async def get_post(self, post_id: str) -> Post:
        parts = post_id.split("/")
        if not re.fullmatch(r"\d+/[0-9a-f]+", post_id):
            raise NotFoundError(f"ehentai 的 post_id 格式应为 gid/token，收到: {post_id}")
        posts = await self._gdata([[parts[0], parts[1]]])
        if not posts:
            raise NotFoundError(f"E-Hentai 找不到画廊 {post_id}")
        return posts[0]

    # ---- 下载目标解析 ----

    async def get_download_targets(self, post: Post) -> list[DownloadTarget]:
        gid = str(post.extra.get("gid") or post.id.split("/")[0])
        token = str(post.extra.get("token") or post.id.split("/")[1])
        page_urls = await self._collect_page_urls(gid, token, post.page_count)
        return [
            DownloadTarget(url, f"{page:03d}.jpg", page=page, resolve=self._resolve_target)
            for page, url in enumerate(page_urls, start=1)
        ]

    async def _resolve_target(self, target: DownloadTarget) -> DownloadTarget:
        image_url = await self._resolve_image_url(target.url)
        ext = urlsplit(image_url).path.rsplit(".", 1)[-1].lower()
        if ext not in IMG_EXTS:
            ext = "jpg"
        return DownloadTarget(
            image_url,
            f"{target.page:03d}.{ext}",
            page=target.page,
            headers={"Referer": target.url, "User-Agent": self._headers()["User-Agent"]},
        )

    async def _collect_page_urls(self, gid: str, token: str, page_count: int) -> list[str]:
        # 用户可配置 20/40/80 等不同缩略图数，必须从实际分页链接遍历。
        found: dict[int, str] = {}
        pending, visited = [0], set()
        while pending:
            p = pending.pop(0)
            if p in visited:
                continue
            visited.add(p)
            html = await self._get_text(f"{self._base()}/g/{gid}/{token}/", params={"p": p})
            soup = BeautifulSoup(html, "html.parser")
            for anchor in soup.find_all("a", href=True):
                parts = urlsplit(urljoin(f"{self._base()}/g/{gid}/{token}/", anchor["href"]))
                match = re.fullmatch(r"/s/([0-9a-f]+)/" + re.escape(gid) + r"-(\d+)", parts.path)
                if match:
                    found[int(match[2])] = self._base() + parts.path
                elif parts.path.rstrip("/") == f"/g/{gid}/{token}":
                    value = parse_qs(parts.query).get("p", [""])[0]
                    if value.isdigit() and int(value) not in visited and int(value) not in pending:
                        pending.append(int(value))
            if page_count and len(found) >= page_count:
                break
            if len(visited) > 1000:
                raise SiteNetworkError("E-Hentai 画廊分页过多")
        if not found or (page_count and any(i not in found for i in range(1, page_count + 1))):
            raise SiteNetworkError(f"E-Hentai 画廊页面不完整：预期 {page_count}，找到 {len(found)}")
        return [found[i] for i in sorted(found) if not page_count or i <= page_count]

    async def _resolve_image_url(self, page_url: str) -> str:
        html = await self._get_text(page_url)
        soup = BeautifulSoup(html, "html.parser")
        img = soup.find("img", id="img")
        src = str(img.get("src", "")) if img else ""
        if not src:
            if "exceeded your image limits" in html.lower():
                raise QuotaError(
                    "E-Hentai 图片配额已用尽",
                    hint="等待配额恢复，或在 config.toml 配置 mirror_base / 更换账号",
                )
            if "just a moment" in html.lower() or "cf-chl" in html or "cf_chl_" in html:
                raise SiteNetworkError(
                    "E-Hentai 图片页返回 Cloudflare 人机验证页",
                    hint="代理出口 IP 可能被 E-Hentai 风控，尝试更换代理节点或稍后再试",
                )
            raise NotFoundError(f"无法解析图片页: {page_url}")
        if "/509" in src:
            raise QuotaError(
                "E-Hentai 图片配额已用尽",
                hint="等待配额恢复，或配置镜像",
            )
        return urljoin(page_url, src)

    # ---- 其他 ----

    def parse_url(self, url: str) -> str | None:
        parts = self._url_parts(url, {"e-hentai.org", "exhentai.org"})
        match = re.fullmatch(r"/g/(\d+)/([0-9a-f]+)/?", parts.path) if parts else None
        return f"{match.group(1)}/{match.group(2)}" if match else None

    async def check(self) -> dict:
        try:
            await self._get_text(f"{self._base()}/")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "detail": f"{self._base()} 可访问"}
