"""应用服务与实际 MCP 协议集成测试。"""

import asyncio
import time

import pytest
from fastmcp import Client

from media_mcp.config import Config
from media_mcp.models import (
    AuthError,
    DownloadedFile,
    DownloadResult,
    DownloadTarget,
    Post,
)
from media_mcp.server import create_server
from media_mcp.service import MediaService


class FakeAdapter:
    name = "fake"

    def __init__(self):
        self.posts = [Post(site="fake", id="1", url="https://fake.example/1")]
        self.targets_count = 1

    async def search(self, query, **kwargs):
        return self.posts

    async def get_post(self, post_id):
        if post_id == "missing":
            raise AuthError("凭证失效", "更新凭证")
        return self.posts[0]

    async def get_download_targets(self, post):
        return [
            DownloadTarget(f"https://fake.example/{i}.jpg", f"{post.id}_{i}.jpg")
            for i in range(self.targets_count)
        ]

    def parse_url(self, url):
        return "1" if url == "https://fake.example/1" else None

    async def check(self):
        return {"ok": True, "detail": "fake ok"}


class FakeDownloader:
    def __init__(self, root):
        self.root = root
        self.calls = []

    async def download(self, post, targets, subdir=None):
        self.calls.append(len(targets))
        return DownloadResult(
            post,
            str(self.root),
            [DownloadedFile(str(self.root / t.filename), t.page, 10) for t in targets],
            str(self.root / "post.json"),
        )


@pytest.fixture
async def service(tmp_path):
    service = MediaService(Config(download_root=tmp_path))
    service.adapters = {"fake": FakeAdapter()}
    service.downloader = FakeDownloader(tmp_path)
    yield service
    await service.close()


async def test_search_and_auth_envelopes(service):
    result = await service.execute("search", site="fake", query="q")
    assert result["success"] and result["data"]["posts"][0]["id"] == "1"
    result = await service.execute("get_post", site="fake", post_id="missing")
    assert result["error"]["type"] == "auth"


async def test_url_route_and_unknown(service):
    result = await service.execute("download_url", url="https://fake.example/1")
    assert result["success"] and len(result["data"]["files"]) == 1
    assert not (await service.execute("download_url", url="https://unknown.example"))["success"]


@pytest.mark.parametrize(
    "options", [{"limit": 0}, {"page": 0}, {"min_score": float("nan")}, {"limit": -1}]
)
async def test_validate_before_network(service, options):
    result = await service.execute("search", site="fake", query="q", **options)
    assert result["error"]["type"] == "validation"


async def test_unknown_site(service):
    assert (await service.execute("search", site="unknown", query="q"))["error"][
        "type"
    ] == "validation"


async def test_timeout_includes_metadata_and_cancels(service):
    cancelled = asyncio.Event()

    async def slow(post_id):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    service.adapters["fake"].get_post = slow
    started = time.monotonic()
    result = await service.execute("download_post", site="fake", post_id="1", timeout=0.03)
    assert result["error"]["type"] == "timeout"
    assert cancelled.is_set() and time.monotonic() - started < 1
    assert not service.downloader.calls


async def test_batch_never_exceeds_file_budget(service):
    adapter = service.adapters["fake"]
    adapter.targets_count = 30
    adapter.posts = [Post(site="fake", id=str(i), url="u") for i in range(3)]
    result = await service.execute("download_search", site="fake", query="q")
    assert result["data"]["total_files"] == 30
    assert service.downloader.calls == [30]
    assert len(result["data"]["skipped"]) == 2
    assert not result["success"]


async def test_large_gallery_skipped_before_resolving(service):
    adapter = service.adapters["fake"]
    adapter.posts[0].page_count = 100

    async def never(post):
        raise AssertionError("should skip before resolving")

    adapter.get_download_targets = never
    result = await service.execute("download_search", site="fake", query="q")
    assert len(result["data"]["skipped"]) == 1
    assert result["data"]["errors"] == []


async def test_batch_timeout_covers_search(service):
    async def slow(*args, **kwargs):
        await asyncio.sleep(10)

    service.adapters["fake"].search = slow
    result = await service.execute("download_search", site="fake", query="q", timeout=0.02)
    assert result["data"]["errors"][0]["type"] == "timeout"
    assert not result["success"]


async def test_mcp_lists_and_calls_tools(service):
    async with Client(create_server(service=service)) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools} == {
            "search",
            "get_post",
            "download_post",
            "download_search",
            "download_url",
            "self_check",
        }
        result = await client.call_tool("search", {"site": "fake", "query": "q"})
        assert result.data["success"]
        assert result.data["data"]["count"] == 1
        check = await client.call_tool("self_check", {})
        assert check.data["data"]["fake"]["ok"]


async def test_errors_redact_credentials(service):
    service.config.sites = {"rule34": {"api_key": "SECRET"}}

    async def error(*args, **kwargs):
        raise AuthError("bad api_key=SECRET")

    service.adapters["fake"].search = error
    result = await service.execute("search", site="fake", query="q")
    assert "SECRET" not in str(result)


async def test_real_stdio_process_from_different_working_directory(tmp_path):
    import sys

    from fastmcp.client.transports import StdioTransport

    config = tmp_path / "isolated.toml"
    config.write_text("[network]\nretries = 1\n", encoding="utf-8")
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "media_mcp.server"],
        env={"MEDIA_HUNTER_CONFIG": str(config)},
        cwd=str(tmp_path),
    )
    async with Client(transport, timeout=15) as client:
        assert len(await client.list_tools()) == 6
        result = await client.call_tool("search", {"site": "unknown", "query": "test"})
        assert result.data["error"]["type"] == "validation"
