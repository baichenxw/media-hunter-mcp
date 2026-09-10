"""共享异步 HTTP：每次尝试限速、有限重试、显式镜像及流式响应。"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import Config
from .models import AuthError, NotFoundError, QuotaError, SiteNetworkError

DEFAULT_INTERVALS = {"e621": 0.5, "rule34": 1.0, "ehentai": 3.0, "pixiv": 1.0}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
API_HOSTS = {
    "e621": {"e621.net", "www.e621.net"},
    "rule34": {"api.rule34.xxx", "rule34.xxx"},
    "ehentai": {"e-hentai.org", "exhentai.org", "api.e-hentai.org"},
    "pixiv": {"app-api.pixiv.net"},
}


class RateLimiter:
    def __init__(self, interval: float):
        self.interval = max(0, interval)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            wait = self._last + self.interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


def _swap_base(url: str, new_base: str) -> str:
    parts, base = urlsplit(url), urlsplit(new_base)
    return urlunsplit(
        (base.scheme, base.netloc, base.path.rstrip("/") + parts.path, parts.query, "")
    )


def check_response(response: httpx.Response, site: str) -> None:
    status = response.status_code
    if status in (401, 403):
        raise AuthError(f"{site} 拒绝访问（HTTP {status}）", "检查凭证或站点访问限制")
    if status == 404:
        raise NotFoundError(f"{site} 资源不存在")
    if status in (429, 509):
        raise QuotaError(f"{site} 请求频率或下载配额受限（HTTP {status}）", "稍后重试")
    if not 200 <= status < 300:
        raise SiteNetworkError(f"{site} 返回 HTTP {status}")


def response_json(response: httpx.Response, site: str):
    check_response(response, site)
    try:
        return response.json()
    except ValueError:
        raise SiteNetworkError(
            f"{site} 返回非 JSON 内容", "可能是验证页面、代理错误或接口变更"
        ) from None


class Network:
    def __init__(self, config: Config):
        self._config = config
        self._limiters: dict[str, RateLimiter] = {}
        self._clients: dict[str, httpx.AsyncClient] = {}

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    def _client(self, use_proxy: bool) -> httpx.AsyncClient:
        proxy = self._config.network.proxy if use_proxy else ""
        key = proxy or "direct"
        if key not in self._clients:
            self._clients[key] = httpx.AsyncClient(
                proxy=proxy or None,
                timeout=self._config.network.timeout,
                follow_redirects=True,
                trust_env=False,
                headers={"User-Agent": "media-hunter-mcp/0.2 (personal media client)"},
            )
        return self._clients[key]

    def _limiter(self, site: str) -> RateLimiter:
        if site not in self._limiters:
            interval = float(
                self._config.site_get(site, "request_interval", DEFAULT_INTERVALS.get(site, 1))
            )
            self._limiters[site] = RateLimiter(interval)
        return self._limiters[site]

    def _attempts(self, site: str, url: str, allow_mirror=True):
        client = self._client(bool(self._config.network.proxy))
        candidates = [(client, url)]
        mirror = self._config.site_get(site, "mirror_base")
        # API 镜像不能套用到任意 CDN 或 OAuth 请求。
        if allow_mirror and mirror and urlsplit(url).hostname in API_HOSTS.get(site, set()):
            candidate = _swap_base(url, mirror)
            if candidate != url:
                candidates.append((client, candidate))
        return candidates

    @staticmethod
    def _backoff(attempt: int, response=None) -> float:
        if response is not None and (value := response.headers.get("Retry-After")):
            try:
                return max(0, min(float(value), 120))
            except ValueError:
                try:
                    return max(
                        0,
                        min(
                            (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds(), 120
                        ),
                    )
                except (TypeError, ValueError):
                    pass
        return min(2**attempt, 8)

    async def _open(self, site, method, url, *, stream=False, allow_mirror=True, **kwargs):
        last_status = None
        for client, candidate in self._attempts(site, url, allow_mirror):
            for attempt in range(self._config.network.retries):
                await self._limiter(site).acquire()
                response = None
                try:
                    request_kwargs = dict(kwargs)
                    auth = request_kwargs.pop("auth", None)
                    request = client.build_request(method, candidate, **request_kwargs)
                    response = await client.send(request, auth=auth, stream=stream)
                    last_status = response.status_code
                    if last_status not in RETRYABLE_STATUS:
                        return response
                except httpx.TransportError:
                    last_status = None
                if response is not None:
                    await response.aclose()
                if attempt + 1 < self._config.network.retries:
                    await asyncio.sleep(self._backoff(attempt, response))
        if last_status == 429:
            raise QuotaError(f"{site} 请求被限流", "已有限重试；请稍后再试")
        raise SiteNetworkError(
            f"{site} 请求失败（连接异常或 HTTP {last_status or '不可用'}）",
            "检查代理、站点连通性或配置的镜像",
        )

    async def request(self, site, method, url, **kwargs) -> httpx.Response:
        return await self._open(site, method, url, **kwargs)

    @asynccontextmanager
    async def stream(self, site, method, url, **kwargs):
        response = await self._open(site, method, url, stream=True, **kwargs)
        try:
            yield response
        finally:
            await response.aclose()
