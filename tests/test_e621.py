"""e621 适配器测试。"""

import httpx
import pytest
import respx

from media_mcp.config import Config, NetworkConfig
from media_mcp.models import MediaType, NotFoundError
from media_mcp.network import Network
from media_mcp.sites.e621 import E621Adapter

E621_POST = {
    "id": 123,
    "tags": {
        "artist": ["foo"],
        "general": ["solo"],
        "species": ["wolf"],
        "character": [],
        "copyright": [],
        "meta": [],
        "lore": [],
        "invalid": [],
    },
    "rating": "s",
    "score": {"up": 50, "down": -8, "total": 42},
    "file": {
        "url": "https://static1.e621.net/data/ab/cd/abcd.jpg",
        "ext": "jpg",
        "md5": "abcdef0123456789abcdef0123456789",
    },
    "preview": {"url": "https://static1.e621.net/data/preview/ab/cd/abcd.jpg"},
}


async def test_search_parses_posts_and_builds_tags():
    config = Config(network=NetworkConfig(retries=1))
    network = Network(config)
    adapter = E621Adapter(config, network)
    with respx.mock:
        route = respx.get("https://e621.net/posts.json").mock(
            return_value=httpx.Response(200, json={"posts": [E621_POST]})
        )
        posts = await adapter.search("solo", limit=5, min_score=10, rating="s")
        request = route.calls.last.request
        assert request.url.params["tags"] == "solo rating:s score:>=10"
        assert request.headers["user-agent"]
    post = posts[0]
    assert post.id == "123"
    assert post.artist == ["foo"]
    assert post.score == 42
    assert post.media_type == MediaType.IMAGE
    assert "wolf" in post.tags and "solo" in post.tags
    await network.close()


async def test_get_post_not_found():
    config = Config(network=NetworkConfig(retries=1))
    network = Network(config)
    adapter = E621Adapter(config, network)
    with respx.mock:
        respx.get("https://e621.net/posts.json").mock(
            return_value=httpx.Response(200, json={"posts": []})
        )
        with pytest.raises(NotFoundError):
            await adapter.get_post("999999")
    await network.close()


async def test_targets_filename_and_parse_url():
    adapter = E621Adapter(Config(), None)
    post = E621Adapter._to_post(E621_POST)
    (target,) = await adapter.get_download_targets(post)
    assert target.filename == "123_foo_abcdef01.jpg"
    assert target.url == E621_POST["file"]["url"]
    assert adapter.parse_url("https://e621.net/posts/123") == "123"
    assert adapter.parse_url("https://e621.net/post/show/123") == "123"
    assert adapter.parse_url("https://www.google.com/") is None
