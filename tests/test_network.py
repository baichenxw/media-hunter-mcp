"""网络层测试：限速、fallback 链、重试策略。"""

import time

import httpx
import pytest
import respx

from media_mcp.config import Config, NetworkConfig
from media_mcp.models import SiteNetworkError
from media_mcp.network import Network, RateLimiter


async def test_rate_limiter_spacing():
    limiter = RateLimiter(0.05)
    start = time.monotonic()
    await limiter.acquire()
    await limiter.acquire()
    assert time.monotonic() - start >= 0.04


async def test_fallback_to_mirror():
    config = Config(
        network=NetworkConfig(retries=1),
        sites={"e621": {"mirror_base": "https://mirror.example"}},
    )
    network = Network(config)
    with respx.mock:
        respx.get("https://e621.net/posts.json").mock(side_effect=httpx.ConnectError("boom"))
        respx.get("https://mirror.example/posts.json").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        resp = await network.request("e621", "GET", "https://e621.net/posts.json")
    assert resp.json() == {"ok": True}
    await network.close()


async def test_4xx_no_retry_returned_as_is():
    config = Config(network=NetworkConfig(retries=3))
    network = Network(config)
    with respx.mock:
        route = respx.get("https://e621.net/posts.json").mock(return_value=httpx.Response(403))
        resp = await network.request("e621", "GET", "https://e621.net/posts.json")
    assert resp.status_code == 403
    assert route.call_count == 1
    await network.close()


async def test_all_candidates_fail_raises():
    config = Config(network=NetworkConfig(retries=1))
    network = Network(config)
    with respx.mock:
        respx.get("https://e621.net/posts.json").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(SiteNetworkError):
            await network.request("e621", "GET", "https://e621.net/posts.json")
    await network.close()


async def test_retries_are_rate_limited():
    config = Config(network=NetworkConfig(retries=2), sites={"e621": {"request_interval": 0.03}})
    network = Network(config)
    with respx.mock:
        route = respx.get("https://e621.net/posts.json").mock(
            side_effect=[
                httpx.Response(503, headers={"retry-after": "0"}),
                httpx.Response(200, json={"posts": []}),
            ]
        )
        start = time.monotonic()
        await network.request("e621", "GET", "https://e621.net/posts.json")
        assert time.monotonic() - start >= 0.025
        assert route.call_count == 2
    await network.close()


async def test_api_mirror_never_rewrites_cdn_or_oauth():
    config = Config(
        network=NetworkConfig(retries=1), sites={"pixiv": {"mirror_base": "https://mirror.example"}}
    )
    network = Network(config)
    assert len(network._attempts("pixiv", "https://i.pximg.net/a.png")) == 1
    assert len(network._attempts("pixiv", "https://oauth.secure.pixiv.net/auth/token")) == 1
    assert len(network._attempts("pixiv", "https://app-api.pixiv.net/v1/illust/detail")) == 2
    await network.close()


async def test_429_maps_to_quota_after_finite_attempts():
    from media_mcp.models import QuotaError

    config = Config(network=NetworkConfig(retries=1))
    network = Network(config)
    with respx.mock:
        route = respx.get("https://e621.net/posts.json").respond(429)
        with pytest.raises(QuotaError):
            await network.request("e621", "GET", "https://e621.net/posts.json")
        assert route.call_count == 1
    await network.close()


def test_mirror_base_preserves_path_prefix():
    from media_mcp.network import _swap_base

    assert (
        _swap_base("https://e621.net/posts.json?q=1", "https://mirror.example/proxy/")
        == "https://mirror.example/proxy/posts.json?q=1"
    )
