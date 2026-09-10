"""E-Hentai 适配器测试。"""

import httpx
import pytest
import respx

from media_mcp.config import Config, NetworkConfig
from media_mcp.models import AuthError, MediaType, Post, QuotaError
from media_mcp.network import Network
from media_mcp.sites.ehentai import EHentaiAdapter

SEARCH_HTML = """
<html><body><table class="itg">
<tr><td><a href="https://e-hentai.org/g/12345/abc123def4/"><img src="t1.jpg"/></a></td></tr>
<tr><td><a href="https://e-hentai.org/g/12345/abc123def4/">dup</a></td></tr>
<tr><td><a href="https://e-hentai.org/g/67890/feedbeef99/"><img src="t2.jpg"/></a></td></tr>
</table></body></html>
"""

GDATA_RESPONSE = {
    "gmetadata": [
        {
            "gid": 12345,
            "token": "abc123def4",
            "title": "Test Gallery",
            "category": "Manga",
            "rating": "4.50",
            "tags": ["artist:someone", "language:chinese"],
            "filecount": "3",
            "thumb": "https://ehgt.org/t.jpg",
            "posted": "1700000000",
        }
    ]
}

IMAGE_PAGE_HTML = (
    '<html><body><div id="i3">'
    '<img id="img" src="https://ehgt.org/img/abc/full.jpg" />'
    "</div></body></html>"
)


def _adapter(**sites):
    config = Config(network=NetworkConfig(retries=1), sites=sites)
    network = Network(config)
    return EHentaiAdapter(config, network), network


def test_parse_search_html_dedup_and_order():
    assert EHentaiAdapter._parse_search_html(SEARCH_HTML) == [
        ["12345", "abc123def4"],
        ["67890", "feedbeef99"],
    ]


async def test_gdata_meta_to_post():
    adapter, network = _adapter()
    with respx.mock:
        route = respx.post("https://api.e-hentai.org/api.php").mock(
            return_value=httpx.Response(200, json=GDATA_RESPONSE)
        )
        posts = await adapter._gdata([["12345", "abc123def4"]])
        import json as _json

        body = _json.loads(route.calls.last.request.content)
        assert body["method"] == "gdata"
        assert body["gidlist"] == [[12345, "abc123def4"]]
    post = posts[0]
    assert post.id == "12345/abc123def4"
    assert post.media_type == MediaType.GALLERY
    assert post.page_count == 3
    assert post.artist == ["someone"]
    assert post.score == 4.5
    assert post.rating == "Manga"
    await network.close()


async def test_resolve_image_url():
    adapter, network = _adapter()
    with respx.mock:
        respx.get("https://e-hentai.org/s/pt/12345-1").mock(
            return_value=httpx.Response(200, text=IMAGE_PAGE_HTML)
        )
        url = await adapter._resolve_image_url("https://e-hentai.org/s/pt/12345-1")
    assert url == "https://ehgt.org/img/abc/full.jpg"
    await network.close()


async def test_resolve_quota_509():
    adapter, network = _adapter()
    with respx.mock:
        respx.get("https://e-hentai.org/s/pt/12345-1").mock(
            return_value=httpx.Response(
                200, text='<img id="img" src="https://e-hentai.org/img/509.gif" />'
            )
        )
        with pytest.raises(QuotaError):
            await adapter._resolve_image_url("https://e-hentai.org/s/pt/12345-1")
    await network.close()


async def test_resolve_cloudflare_challenge_raises_network():
    adapter, network = _adapter()
    with respx.mock:
        respx.get("https://e-hentai.org/s/pt/12345-1").mock(
            return_value=httpx.Response(
                200, text="<html>Just a moment...<script>cf-chl</script></html>"
            )
        )
        with pytest.raises(Exception) as exc_info:
            await adapter._resolve_image_url("https://e-hentai.org/s/pt/12345-1")
        assert "Cloudflare" in str(exc_info.value)
    await network.close()


async def test_get_download_targets_resolves_just_before_download():
    adapter, network = _adapter()
    post = Post(
        site="ehentai",
        id="12345/abc123def4",
        url="u",
        page_count=2,
        extra={"gid": "12345", "token": "abc123def4"},
    )
    with respx.mock:
        respx.get("https://e-hentai.org/g/12345/abc123def4/").mock(
            return_value=httpx.Response(
                200,
                text='<a href="https://e-hentai.org/s/aa/12345-1"><a href="https://e-hentai.org/s/bb/12345-2">',
            )
        )
        respx.get("https://e-hentai.org/s/aa/12345-1").mock(
            return_value=httpx.Response(
                200, text='<img id="img" src="https://ehgt.org/a/1.webp" />'
            )
        )
        respx.get("https://e-hentai.org/s/bb/12345-2").mock(
            return_value=httpx.Response(200, text='<img id="img" src="https://ehgt.org/b/2.jpg" />')
        )
        targets = await adapter.get_download_targets(post)
        assert all(t.resolve is not None for t in targets)
        targets = [await t.resolve(t) for t in targets]
    assert [t.filename for t in targets] == ["001.webp", "002.jpg"]
    assert all(t.headers["Referer"].startswith("https://e-hentai.org/s/") for t in targets)
    await network.close()


async def test_sad_panda_raises_auth():
    adapter, network = _adapter(
        ehentai={"use_exhentai": True, "cookie": "ipb_member_id=1; ipb_pass_hash=x"}
    )
    with respx.mock:
        respx.get("https://exhentai.org/").mock(
            return_value=httpx.Response(
                200, content=b"GIF89a....", headers={"content-type": "image/gif"}
            )
        )
        with pytest.raises(AuthError):
            await adapter._get_text("https://exhentai.org/")
    await network.close()


def test_parse_url():
    adapter, _ = _adapter()
    assert adapter.parse_url("https://e-hentai.org/g/12345/abc123def4/") == "12345/abc123def4"
    assert adapter.parse_url("https://exhentai.org/g/12345/abc123def4/") == "12345/abc123def4"
    assert adapter.parse_url("https://e621.net/posts/1") is None


async def test_search_cursor_pagination_and_category():
    config = Config(network=NetworkConfig(retries=1), sites={"ehentai": {"request_interval": 0}})
    network = Network(config)
    adapter = EHentaiAdapter(config, network)
    with respx.mock:
        route = respx.get("https://e-hentai.org/").mock(
            side_effect=[
                httpx.Response(
                    200,
                    text='<a id="dnext" href="/?f_cats=767&f_search=landscape&next=123">Next</a>',
                ),
                httpx.Response(200, text=SEARCH_HTML),
            ]
        )
        respx.post("https://api.e-hentai.org/api.php").respond(
            200, json={"gmetadata": [{**GDATA_RESPONSE["gmetadata"][0], "category": "Non-H"}]}
        )
        posts = await adapter.search("landscape", page=2, rating="Non-H")
        assert route.calls[0].request.url.params["f_cats"] == "767"
        assert route.calls[1].request.url.params["next"] == "123"
        assert len(posts) == 1
    await network.close()


async def test_gallery_follows_actual_thumbnail_pages():
    config = Config(network=NetworkConfig(retries=1), sites={"ehentai": {"request_interval": 0}})
    network = Network(config)
    adapter = EHentaiAdapter(config, network)
    with respx.mock:
        route = respx.get("https://e-hentai.org/g/123/abc/").mock(
            side_effect=[
                httpx.Response(
                    200,
                    text='<a href="/s/aa/123-1">1</a><a href="/s/bb/123-2">2</a><a href="?p=1">next</a>',
                ),
                httpx.Response(
                    200,
                    text='<a href="/s/cc/123-3">3</a><a href="/s/dd/123-4">4</a><a href="?p=0">prev</a>',
                ),
            ]
        )
        urls = await adapter._collect_page_urls("123", "abc", 4)
        assert len(urls) == 4
        assert route.call_count == 2
    await network.close()


async def test_incomplete_gallery_not_reported_complete():
    from media_mcp.models import SiteNetworkError

    config = Config(network=NetworkConfig(retries=1), sites={"ehentai": {"request_interval": 0}})
    network = Network(config)
    adapter = EHentaiAdapter(config, network)
    with respx.mock:
        respx.get("https://e-hentai.org/g/123/abc/").respond(
            200, text='<a href="/s/aa/123-1">1</a>'
        )
        with pytest.raises(SiteNetworkError, match="不完整"):
            await adapter._collect_page_urls("123", "abc", 3)
    await network.close()
