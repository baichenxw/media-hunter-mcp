"""rule34 适配器测试。"""

import httpx
import pytest
import respx

from media_mcp.config import Config, NetworkConfig
from media_mcp.models import AuthError, MediaType
from media_mcp.network import Network
from media_mcp.sites.rule34 import Rule34Adapter

RULE34_POST = {
    "id": 456,
    "tags": "tag1 tag2",
    "owner": "someone",
    "file_url": "https://wimg.rule34.xxx/images/abcd/a.mp4",
    "preview_url": "https://rule34.xxx/thumbnails/abcd/thumb.jpg",
    "score": 15,
    "rating": "explicit",
    "md5": "0123456789abcdef0123456789abcdef",
}


def _adapter(**sites):
    config = Config(network=NetworkConfig(retries=1), sites=sites)
    network = Network(config)
    return Rule34Adapter(config, network), network


async def test_missing_credentials_raises_auth():
    adapter, network = _adapter()
    with pytest.raises(AuthError):
        await adapter.search("tag1")
    await network.close()


async def test_search_builds_params_and_parses():
    adapter, network = _adapter(rule34={"user_id": "1", "api_key": "k"})
    with respx.mock:
        route = respx.get("https://api.rule34.xxx/index.php").mock(
            return_value=httpx.Response(200, json=[RULE34_POST])
        )
        posts = await adapter.search("tag1", limit=3, page=2)
        params = route.calls.last.request.url.params
        assert params["user_id"] == "1"
        assert params["api_key"] == "k"
        assert params["pid"] == "1"
        assert params["json"] == "1"
        assert params["tags"] == "tag1"
    post = posts[0]
    assert post.media_type == MediaType.VIDEO
    assert post.rating == "explicit"
    assert post.score == 15
    assert post.tags == ["tag1", "tag2"]
    await network.close()


async def test_targets_and_parse_url():
    adapter, _ = _adapter(rule34={"user_id": "1", "api_key": "k"})
    post = Rule34Adapter._to_post(RULE34_POST)
    (target,) = await adapter.get_download_targets(post)
    assert target.filename == "456_01234567.mp4"
    assert adapter.parse_url("https://rule34.xxx/index.php?page=post&s=view&id=456") == "456"
    assert adapter.parse_url("https://e621.net/posts/1") is None


async def test_targets_filename_without_md5():
    adapter, _ = _adapter(rule34={"user_id": "1", "api_key": "k"})
    raw = dict(RULE34_POST, md5="")
    post = Rule34Adapter._to_post(raw)
    (target,) = await adapter.get_download_targets(post)
    assert target.filename == "456.mp4"


async def test_search_empty_body_returns_empty():
    adapter, network = _adapter(rule34={"user_id": "1", "api_key": "k"})
    with respx.mock:
        respx.get("https://api.rule34.xxx/index.php").mock(
            return_value=httpx.Response(200, text="", headers={"content-type": "application/json"})
        )
        posts = await adapter.search("catgirl rating:safe")
        assert posts == []
    await network.close()


async def test_search_html_body_raises_network():
    adapter, network = _adapter(rule34={"user_id": "1", "api_key": "k"})
    with respx.mock:
        respx.get("https://api.rule34.xxx/index.php").mock(
            return_value=httpx.Response(
                200, text="<html>error</html>", headers={"content-type": "text/html"}
            )
        )
        with pytest.raises(Exception) as exc_info:
            await adapter.search("catgirl")
        assert "rule34" in str(exc_info.value)
    await network.close()
