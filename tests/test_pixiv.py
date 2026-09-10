"""Pixiv OAuth 与 API 回归测试，不触网。"""

import asyncio

import httpx
import pytest
import respx

from media_mcp.config import Config, NetworkConfig
from media_mcp.models import AuthError, MediaType, Post, QuotaError
from media_mcp.network import Network
from media_mcp.sites.pixiv import PixivAdapter

AUTH = "https://oauth.secure.pixiv.net/auth/token"
API = "https://app-api.pixiv.net"
ILLUST = {
    "id": 777,
    "title": "测试图",
    "type": "illust",
    "page_count": 2,
    "x_restrict": 1,
    "total_bookmarks": 99,
    "tags": [{"name": "tagA"}],
    "user": {"id": 8, "name": "画师B"},
    "image_urls": {"medium": "https://i.pximg.net/m.jpg"},
    "meta_pages": [{"image_urls": {"original": f"https://i.pximg.net/p{i}.png"}} for i in range(2)],
}


@pytest.fixture
async def adapter():
    config = Config(
        network=NetworkConfig(retries=1),
        sites={"pixiv": {"refresh_token": "test", "request_interval": 0}},
    )
    network = Network(config)
    with respx.mock as router:
        router.post(AUTH).respond(
            200, json={"access_token": "access", "refresh_token": "refreshed", "expires_in": 3600}
        )
        yield PixivAdapter(config, network), router
    await network.close()


def test_to_post():
    post = PixivAdapter._to_post(ILLUST)
    assert post.media_type == MediaType.GALLERY and post.artist == ["画师B"]
    assert post.rating == "R-18" and post.score == 99
    assert PixivAdapter._to_post({**ILLUST, "x_restrict": 2}).rating == "R-18G"


async def test_multi_page_targets(adapter):
    site, router = adapter
    router.get(API + "/v1/illust/detail").respond(200, json={"illust": ILLUST})
    targets = await site.get_download_targets(PixivAdapter._to_post(ILLUST))
    assert [t.filename for t in targets] == ["777_p0.png", "777_p1.png"]
    assert all(t.headers["Referer"] == "https://pixiv.net" for t in targets)


async def test_ugoira_medium_zip_fallback(adapter):
    site, router = adapter
    router.get(API + "/v1/ugoira/metadata").respond(
        200,
        json={
            "ugoira_metadata": {
                "zip_urls": {"medium": "https://i.pximg.net/u.zip"},
                "frames": [{"file": "000.jpg", "delay": 90}],
            }
        },
    )
    targets = await site.get_download_targets(
        Post(site="pixiv", id="778", url="u", media_type=MediaType.UGOIRA)
    )
    assert targets[0].post_process_meta["frames"][0]["delay"] == 90
    assert targets[0].url.endswith("u.zip")


async def test_parallel_auth_once(adapter):
    site, router = adapter
    router.get(API + "/v1/illust/detail").respond(200, json={"illust": ILLUST})
    await asyncio.gather(site.get_post("777"), site.get_post("777"))
    assert router.routes[0].call_count == 1


async def test_expired_access_token_refreshes_once(adapter):
    site, router = adapter
    route = router.get(API + "/v1/illust/detail").mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "OAuth invalid_grant"}}),
            httpx.Response(200, json={"illust": ILLUST}),
        ]
    )
    assert (await site.get_post("777")).id == "777"
    assert route.call_count == 2 and router.routes[0].call_count == 2


async def test_error_object_not_empty_success(adapter):
    site, router = adapter
    router.get(API + "/v1/search/illust").respond(200, json={"error": {"message": "Rate Limit"}})
    with pytest.raises(QuotaError):
        await site.search("q")


async def test_rating_exact_match(adapter):
    site, router = adapter
    router.get(API + "/v1/search/illust").respond(
        200, json={"illusts": [ILLUST, {**ILLUST, "id": 778, "x_restrict": 2}]}
    )
    assert [p.id for p in await site.search("q", rating="r18")] == ["777"]
    assert len(await site.search("q", rating="all")) == 2


async def test_missing_refresh_token():
    site = PixivAdapter(Config(), None)
    with pytest.raises(AuthError):
        await site._authenticate()


def test_urls_and_image_mirror():
    site = PixivAdapter(Config(sites={"pixiv": {"image_mirror": "https://mirror.example"}}), None)
    assert site.parse_url("https://www.pixiv.net/en/artworks/777") == "777"
    assert site.parse_url("https://www.pixiv.net/member_illust.php?illust_id=777") == "777"
    assert site.parse_url("https://fakepixiv.net/artworks/777") is None
    assert site.parse_url("https://evil.example/?url=https://pixiv.net/artworks/777") is None
    assert site._rewrite_image_url("https://i.pximg.net/a.png") == "https://mirror.example/a.png"
    assert (
        site._rewrite_image_url("https://i.pximg.net.evil.example/a.png")
        == "https://i.pximg.net.evil.example/a.png"
    )
