"""E-Hentai / ExHentai 适配器：网页搜索 + api.php 元数据 + 画廊页解析下载。"""

from __future__ import annotations

import asyncio
import re
from email.message import Message
from functools import partial
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .. import __version__
from ..models import (
    AuthError,
    DownloadTarget,
    MediaType,
    NotFoundError,
    Post,
    QuotaError,
    SiteNetworkError,
    ValidationError,
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

    def __init__(self, config, network, *, use_exhentai=None, original=False):
        super().__init__(config, network)
        self.use_exhentai = (
            config.site_get(self.name, "use_exhentai", True)
            if use_exhentai is None
            else use_exhentai
        )
        self.original = original
        if original and not config.site_get(self.name, "allow_original", True):
            raise ValidationError("配置禁止下载原图", "将 sites.ehentai.allow_original 设为 true")

    def with_options(self, *, use_exhentai=None, original=False):
        # 每次调用独立实例，共享网络限速器，不修改服务中其他调用的状态。
        return EHentaiAdapter(
            self._config, self._network, use_exhentai=use_exhentai, original=original
        )

    def _mirror(self):
        key = "exhentai_mirror_base" if self.use_exhentai else "mirror_base"
        return str(self._config.site_get(self.name, key, "") or "").rstrip("/")

    # ---- 基础设施 ----

    def _cookie(self) -> str:
        return str(self._config.site_get("ehentai", "cookie", "") or "")

    def _base(self) -> str:
        if self.use_exhentai and not self._cookie():
            raise AuthError(
                "ExHentai 需要 cookie", "填写 cookie，或本次调用设置 use_exhentai=false"
            )
        mirror = self._mirror()
        if mirror:
            return mirror.rstrip("/")
        if self.use_exhentai:
            return "https://exhentai.org"
        return "https://e-hentai.org"

    def _api_url(self) -> str:
        self._base()  # 在发送任何请求前校验里站凭证。
        mirror = self._mirror()
        if mirror:
            return f"{mirror.rstrip('/')}/api.php"
        if self._base().endswith("exhentai.org"):
            return "https://exhentai.org/api.php"
        return "https://api.e-hentai.org/api.php"

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": f"media-hunter-mcp/{__version__} (gallery metadata client)"}
        cookie = self._cookie()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _get_text(self, url: str, params: dict | None = None) -> str:
        response = await self._network.request(
            self.name, "GET", url, params=params, headers=self._headers(), allow_mirror=False
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
                allow_mirror=False,
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
            extra={
                "gid": gid,
                "token": token,
                "posted": meta.get("posted", ""),
                "use_exhentai": self.use_exhentai,
            },
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
        post.extra["original"] = self.original
        gid = str(post.extra.get("gid") or post.id.split("/")[0])
        token = str(post.extra.get("token") or post.id.split("/")[1])
        page_urls = await self._collect_page_urls(gid, token, post.page_count)
        return [
            DownloadTarget(
                url,
                f"{page:03d}.jpg",
                page=page,
                resolve=self._resolve_target,
                post_process_meta={"original": True} if self.original else {},
            )
            for page, url in enumerate(page_urls, start=1)
        ]

    async def _resolve_target(self, target: DownloadTarget) -> DownloadTarget:
        image_url = await self._resolve_image_url(target.url, original=self.original)
        ext = urlsplit(image_url).path.rsplit(".", 1)[-1].lower()
        if ext not in IMG_EXTS:
            ext = "jpg"
        headers = {"Referer": target.url, "User-Agent": self._headers()["User-Agent"]}
        image_parts = urlsplit(image_url)
        if (
            self.original
            and image_parts.netloc == urlsplit(self._base()).netloc
            and image_parts.scheme == urlsplit(self._base()).scheme
            and re.search(r"/fullimg(?:\.php)?(?:/|$)", image_parts.path)
        ):
            headers.update(self._headers())
        return DownloadTarget(
            image_url,
            f"{target.page:03d}.{ext}",
            page=target.page,
            headers=headers,
            response_filename=partial(self._original_filename, page=target.page)
            if self.original
            else None,
        )

    async def _collect_page_urls(self, gid: str, token: str, page_count: int) -> list[str]:
        # 用户可配置 20/40/80 等不同缩略图数，必须从实际分页链接遍历。
        found: dict[int, str] = {}
        base = self._base()
        prefix = urlsplit(base).path.rstrip("/")
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
                # 镜像既可能保留原站链接，也可能将链接改写到自己的路径前缀下。
                path = parts.path
                if prefix and path.startswith(prefix + "/"):
                    path = path[len(prefix) :]
                match = re.fullmatch(r"/s/([0-9a-f]+)/" + re.escape(gid) + r"-(\d+)", path)
                if match:
                    found[int(match[2])] = base + path
                elif path.rstrip("/") == f"/g/{gid}/{token}":
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

    async def _resolve_image_url(self, page_url: str, *, original=False) -> str:
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
        if original:
            for anchor in soup.find_all("a", href=True):
                parts = urlsplit(urljoin(page_url, anchor["href"]))
                endpoint = re.search(r"/fullimg(?:\.php)?(?:/|$)", parts.path)
                if not endpoint:
                    continue
                base = self._base()
                if (
                    parts.scheme not in {"http", "https"}
                    or parts.username
                    or parts.netloc not in {"e-hentai.org", "exhentai.org", urlsplit(base).netloc}
                ):
                    raise SiteNetworkError("原图入口地址不可信")
                if not self._cookie():
                    raise AuthError("下载原图需要站点 Cookie", "填写 sites.ehentai.cookie")
                path = parts.path[endpoint.start() :]
                return f"{base}{path}" + (f"?{parts.query}" if parts.query else "")
            visible_text = soup.get_text(" ", strip=True).lower()
            if "resampled" in visible_text or "download original" in visible_text:
                raise NotFoundError(
                    "图片经过缩放，但页面未提供原图入口", "检查账号权限或选择 original=false"
                )
            # 未缩放的图片没有单独的 fullimg 入口，页面图本身就是源图。
        return urljoin(page_url, src)

    @staticmethod
    async def _original_filename(response, *, page):
        """原图入口可重定向或直接响应；只消费同一次 GET，错误页不落盘。"""
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if "/509" in response.url.path:
            raise QuotaError("E-Hentai 原图额度不足")
        if content_type.startswith("text/"):
            data = bytearray()
            async for chunk in response.aiter_bytes(4096):
                data.extend(chunk[: 65536 - len(data)])
                if len(data) >= 65536:
                    break
            text = data.decode("utf-8", errors="replace").lower()
            if any(
                term in text
                for term in (
                    "exceeded",
                    "insufficient",
                    "not enough",
                    "not have enough",
                    "quota",
                    "temporarily banned",
                )
            ):
                raise QuotaError(
                    "E-Hentai 原图额度或 GP 不足", "查看站点额度，或选择 original=false"
                )
            if any(term in text for term in ("log in", "login", "not logged", "sad panda")):
                raise AuthError("E-Hentai 原图下载需要有效登录", "更新 Cookie 或检查访问权限")
            raise SiteNetworkError("原图入口返回错误页面，未保存为原图")
        types = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/gif": "gif",
            "image/webp": "webp",
            "image/avif": "avif",
            "image/jxl": "jxl",
            "image/bmp": "bmp",
            "image/tiff": "tiff",
        }
        message = Message()
        message["Content-Disposition"] = response.headers.get("content-disposition", "")
        filename = message.get_filename() or response.url.path
        extension = filename.rsplit(".", 1)[-1].lower()
        extension = types.get(content_type, extension)
        if extension not in set(types.values()) | {"jpeg"} or content_type not in {
            *types,
            "application/octet-stream",
            "",
        }:
            raise SiteNetworkError("原图响应不是可识别的图片")
        return f"{page:03d}.{extension}"

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
