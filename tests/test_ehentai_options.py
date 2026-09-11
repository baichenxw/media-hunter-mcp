"""E 站调用级选择与原图入口的离线回归测试。"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
from fastmcp import Client

from media_mcp.config import Config, NetworkConfig
from media_mcp.models import AuthError, SiteNetworkError, ValidationError
from media_mcp.network import Network
from media_mcp.server import create_server
from media_mcp.service import MediaService
from media_mcp.sites.ehentai import EHentaiAdapter

COOKIE = "ipb_member_id=123; ipb_pass_hash=test-cookie-value; igneous=test-igneous"
META = {"gmetadata": [{"gid": 12, "token": "abc", "title": "gallery", "filecount": "1"}]}


@pytest.fixture
async def service(tmp_path):
    service = MediaService(
        Config(
            download_root=tmp_path,
            network=NetworkConfig(retries=1),
            sites={"ehentai": {"cookie": COOKIE, "request_interval": 0, "download_concurrency": 1}},
        )
    )
    yield service
    await service.close()


def gallery_routes(host="https://exhentai.org", original=True):
    api = host + "/api.php" if "exhentai" in host else "https://api.e-hentai.org/api.php"
    respx.post(api).respond(200, json=META)
    respx.get(host + "/g/12/abc/").respond(200, text='<a href="/s/aa/12-1">1</a>')
    link = (
        '<a href="/fullimg.php?gid=12&page=1&key=abc">Download original source</a>'
        if original
        else ""
    )
    return respx.get(host + "/s/aa/12-1").respond(
        200,
        text='<img id="img" src="https://cdn.example/resampled.jpg">' + link,
    )


def test_default_private_requires_cookie_but_public_is_selectable():
    config = Config()
    network = Network(config)
    adapter = EHentaiAdapter(config, network)
    assert adapter.use_exhentai
    with pytest.raises(AuthError):
        adapter._base()
    assert adapter.with_options(use_exhentai=False)._base() == "https://e-hentai.org"
    assert adapter.use_exhentai  # no shared mutation


@pytest.mark.parametrize("field", ["use_exhentai", "allow_original"])
def test_boolean_config_validation(field):
    with pytest.raises(ValidationError, match=field):
        Config(sites={"ehentai": {field: "true"}})


async def test_disabled_original_fails_before_network(tmp_path):
    service = MediaService(
        Config(download_root=tmp_path, sites={"ehentai": {"allow_original": False}})
    )
    try:
        with respx.mock(assert_all_called=False) as router:
            result = await service.execute(
                "download_post", site="ehentai", post_id="12/abc", original=True
            )
            assert result["error"]["type"] == "validation"
            assert not router.calls
    finally:
        await service.close()


@pytest.mark.parametrize(
    "operation,args",
    [
        ("search", {"query": "landscape"}),
        ("get_post", {"post_id": "12"}),
        ("download_post", {"post_id": "12"}),
        ("download_search", {"query": "landscape"}),
    ],
)
async def test_ehentai_option_rejected_for_other_sites(service, operation, args):
    with respx.mock:
        result = await service.execute(operation, site="e621", use_exhentai=False, **args)
        assert result["error"]["type"] == "validation"


async def test_concurrent_public_and_private_metadata_are_isolated(service):
    with respx.mock:
        public = respx.post("https://api.e-hentai.org/api.php").respond(200, json=META)
        private = respx.post("https://exhentai.org/api.php").respond(200, json=META)
        results = await asyncio.gather(
            *(
                service.execute("get_post", site="ehentai", post_id="12/abc", use_exhentai=choice)
                for choice in [False, True, False, True]
            )
        )
        assert public.call_count == private.call_count == 2
        for result, expected in zip(results, [False, True, False, True]):
            assert result["success"]
            assert result["data"]["extra"]["use_exhentai"] is expected
            assert result["data"]["url"].startswith(
                "https://exhentai.org" if expected else "https://e-hentai.org"
            )
        assert service.adapters["ehentai"].use_exhentai is True


@pytest.mark.parametrize("original", [False, True])
async def test_url_host_controls_default_and_original_download(service, original):
    with respx.mock:
        gallery_routes("https://e-hentai.org")
        if original:
            entry = respx.get("https://e-hentai.org/fullimg.php").respond(
                302,
                headers={"location": "https://cdn.example/original.png"},
            )
            media = respx.get("https://cdn.example/original.png").respond(
                200,
                content=b"original-png",
                headers={"content-type": "image/png"},
            )
        else:
            media = respx.get("https://cdn.example/resampled.jpg").respond(
                200,
                content=b"resampled",
                headers={"content-type": "image/jpeg"},
            )
        result = await service.execute(
            "download_url", url="https://e-hentai.org/g/12/abc/", original=original
        )
        assert result["success"], result
        assert not result["data"]["post"]["extra"]["use_exhentai"]
        path = Path(result["data"]["files"][0]["path"])
        assert path.name == ("001.png" if original else "001.jpg")
        assert (path.parent.name == "original") is original
        assert "cookie" not in media.calls.last.request.headers
        if original:
            assert entry.call_count == 1
            assert entry.calls.last.request.headers["cookie"] == COOKIE


async def test_explicit_private_overrides_public_url(service):
    with respx.mock:
        gallery_routes()
        respx.get("https://cdn.example/resampled.jpg").respond(200, content=b"image")
        result = await service.execute(
            "download_url", url="https://e-hentai.org/g/12/abc/", use_exhentai=True
        )
        assert result["success"] and result["data"]["post"]["extra"]["use_exhentai"]


async def test_original_direct_response_uses_content_type_and_only_one_get(service):
    with respx.mock:
        gallery_routes()
        entry = respx.get("https://exhentai.org/fullimg.php").respond(
            200,
            content=b"original-data",
            headers={"content-type": "image/webp"},
        )
        result = await service.execute(
            "download_post", site="ehentai", post_id="12/abc", original=True
        )
        assert result["success"], result
        assert entry.call_count == 1
        assert Path(result["data"]["files"][0]["path"]).name == "001.webp"


async def test_original_falls_back_only_when_page_image_is_not_resampled(service):
    with respx.mock:
        gallery_routes(original=False)
        respx.get("https://cdn.example/resampled.jpg").respond(
            200, content=b"image", headers={"content-type": "image/jpeg"}
        )
        result = await service.execute(
            "download_post", site="ehentai", post_id="12/abc", original=True
        )
        assert result["success"]
        respx.get("https://exhentai.org/s/aa/12-1").respond(
            200, text='<img id="img" src="https://cdn.example/resampled.jpg">Resampled'
        )
        result = await service.execute(
            "download_post", site="ehentai", post_id="12/abc", original=True, overwrite=True
        )
        assert not result["success"]
        assert result["data"]["errors"][0]["type"] == "not_found"


async def test_original_permission_error_stops_batch_without_writing_html(service):
    with respx.mock:
        respx.get("https://exhentai.org/").respond(
            200, text='<a href="/g/12/abc/">1</a><a href="/g/13/def/">2</a>'
        )
        gallery_routes()
        respx.post("https://exhentai.org/api.php").respond(
            200,
            json={
                "gmetadata": [
                    META["gmetadata"][0],
                    {"gid": 13, "token": "def", "title": "second", "filecount": "1"},
                ]
            },
        )
        entry = respx.get("https://exhentai.org/fullimg.php").respond(
            200, text="You do not have enough GP to download this image."
        )
        result = await service.execute(
            "download_search", site="ehentai", query="landscape", original=True
        )
        assert not result["success"]
        assert result["data"]["stop_reason"]["type"] == "quota"
        assert result["data"]["skipped"][0]["id"] == "13/def"
        assert result["data"]["total_files"] == 0 and entry.call_count == 1
        assert not list(service.config.download_root.rglob("*.part"))
        assert not list(service.config.download_root.rglob("*.jpg"))


async def test_original_and_resampled_files_have_independent_resume_records(service):
    with respx.mock:
        pages = gallery_routes()
        small = respx.get("https://cdn.example/resampled.jpg").respond(200, content=b"small")
        large = respx.get("https://exhentai.org/fullimg.php").respond(
            200, content=b"large", headers={"content-type": "image/jpeg"}
        )
        results = []
        for original in [False, True, False, True]:
            results.append(
                await service.execute(
                    "download_post", site="ehentai", post_id="12/abc", original=original
                )
            )
        assert all(r["success"] for r in results)
        assert [r["data"]["reused_files"] for r in results] == [0, 0, 1, 1]
        assert pages.call_count == 2 and small.call_count == large.call_count == 1
        files = [r["data"]["files"][0] for r in results[:2]]
        assert files[0]["path"] != files[1]["path"]
        assert files[0]["source_key"] != files[1]["source_key"]
        assert Path(files[0]["path"]).read_bytes() == b"small"
        assert Path(files[1]["path"]).read_bytes() == b"large"


async def test_separate_mirror_routing_and_original_entry(service):
    service.config.sites["ehentai"].update(
        mirror_base="https://public.example/eh", exhentai_mirror_base="https://private.example/ex"
    )
    with respx.mock:
        for private, base in [
            (False, "https://public.example/eh"),
            (True, "https://private.example/ex"),
        ]:
            adapter = service.adapter("ehentai", use_exhentai=private, original=True)
            assert adapter._api_url() == base + "/api.php"
            respx.get(base + "/s/aa/12-1").respond(
                200,
                text='<img id="img" src="https://cdn.example/1.jpg"><a href="https://exhentai.org/fullimg.php?gid=12&page=1&key=x">Original</a>',
            )
            assert (
                await adapter._resolve_image_url(base + "/s/aa/12-1", original=True)
                == base + "/fullimg.php?gid=12&page=1&key=x"
            )


async def test_foreign_original_entry_rejected(service):
    with respx.mock:
        respx.get("https://exhentai.org/s/aa/12-1").respond(
            200,
            text='<img id="img" src="https://cdn.example/1.jpg"><a href="https://evil.example/fullimg.php?key=x">Original</a>',
        )
        with pytest.raises(SiteNetworkError, match="不可信"):
            await service.adapter("ehentai")._resolve_image_url(
                "https://exhentai.org/s/aa/12-1", original=True
            )


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_mcp_schema_and_actual_call_options(service, mode):
    with respx.mock:
        respx.post("https://api.e-hentai.org/api.php").respond(200, json=META)
        async with Client(create_server(service=service), mode=mode) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            for name in tools:
                props = tools[name].input_schema["properties"]
                assert "use_exhentai" in props
                if name.startswith("download_"):
                    assert (
                        props["original"]["type"] == "boolean"
                        and props["original"]["default"] is False
                    )
            result = await client.call_tool(
                "get_post", {"site": "ehentai", "post_id": "12/abc", "use_exhentai": False}
            )
            assert json.loads(result.content[0].text)["data"]["extra"]["use_exhentai"] is False


@pytest.mark.parametrize(
    "suffix", ["/fullimg/12/1/key/source.png", "/fullimg.php/source.png?gid=12&page=1&key=x"]
)
async def test_rewritten_original_entry_preserves_path(service, suffix):
    with respx.mock:
        respx.get("https://exhentai.org/s/aa/12-1").respond(
            200,
            text='<img id="img" src="https://cdn.example/a.jpg"><a href="'
            + suffix
            + '">Download original source</a>',
        )
        adapter = service.adapter("ehentai", original=True)
        assert (
            await adapter._resolve_image_url("https://exhentai.org/s/aa/12-1", original=True)
            == "https://exhentai.org" + suffix
        )


async def test_download_mcp_passes_both_options(service):
    with respx.mock:
        gallery_routes("https://e-hentai.org")
        respx.get("https://e-hentai.org/fullimg.php").respond(
            200, content=b"source", headers={"content-type": "image/png"}
        )
        async with Client(create_server(service=service)) as client:
            result = await client.call_tool(
                "download_post",
                {"site": "ehentai", "post_id": "12/abc", "use_exhentai": False, "original": True},
            )
            data = json.loads(result.content[0].text)["data"]
            assert data["post"]["extra"]["original"]
            assert not data["post"]["extra"]["use_exhentai"]
            assert Path(data["files"][0]["path"]).name == "001.png"


async def test_original_timeout_cleans_partial_stream(service):
    closed = asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first bytes"
            await asyncio.sleep(20)

        async def aclose(self):
            closed.set()

    with respx.mock:
        gallery_routes()
        respx.get("https://exhentai.org/fullimg.php").mock(
            return_value=httpx.Response(
                200, stream=SlowStream(), headers={"content-type": "image/png"}
            )
        )
        result = await service.execute(
            "download_post", site="ehentai", post_id="12/abc", original=True, timeout=0.1
        )
        assert result["error"]["type"] == "timeout"
        assert closed.is_set()
        assert not list(service.config.download_root.rglob("*.part"))
        assert not list(service.config.download_root.rglob("*.png"))


@pytest.mark.parametrize("use_exhentai", [True, False])
async def test_search_selects_requested_host(service, use_exhentai):
    host = "https://exhentai.org" if use_exhentai else "https://e-hentai.org"
    api = host + "/api.php" if use_exhentai else "https://api.e-hentai.org/api.php"
    with respx.mock:
        respx.get(host + "/").respond(200, text='<a href="/g/12/abc/">gallery</a>')
        respx.post(api).respond(200, json=META)
        result = await service.execute(
            "search", site="ehentai", query="landscape", use_exhentai=use_exhentai
        )
        assert result["success"]
        assert result["data"]["posts"][0]["url"].startswith(host)


async def test_binary_original_content_disposition_extension(service):
    with respx.mock:
        gallery_routes()
        respx.get("https://exhentai.org/fullimg.php").respond(
            200,
            content=b"source",
            headers={
                "content-type": "application/octet-stream",
                "content-disposition": "attachment; filename=original.tiff",
            },
        )
        result = await service.execute(
            "download_post", site="ehentai", post_id="12/abc", original=True
        )
        assert result["success"]
        assert Path(result["data"]["files"][0]["path"]).name == "001.tiff"


@pytest.mark.parametrize("original", [False, True])
async def test_media_url_named_fullimg_never_receives_cookie_on_foreign_host(service, original):
    with respx.mock:
        gallery_routes(original=False)
        respx.get("https://exhentai.org/s/aa/12-1").respond(
            200, text='<img id="img" src="https://cdn.example/fullimg.php">'
        )
        media = respx.get("https://cdn.example/fullimg.php").respond(
            200, content=b"image", headers={"content-type": "image/jpeg"}
        )
        result = await service.execute(
            "download_post", site="ehentai", post_id="12/abc", original=original
        )
        assert result["success"]
        assert "cookie" not in media.calls.last.request.headers
