"""应用服务与实际 MCP 协议集成测试。"""

import asyncio
import json
import os
import sys
import time

import httpx
import pytest
from fastmcp import Client

from media_mcp.config import Config
from media_mcp.models import (
    AuthError,
    DownloadedFile,
    DownloadResult,
    DownloadTarget,
    Post,
    QuotaError,
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

    async def download(self, post, targets, subdir=None, *, overwrite=False):
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


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_mcp_lists_and_calls_tools(service, mode):
    async with Client(create_server(service=service), mode=mode) as client:
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
        assert result.structured_content["success"]
        assert result.structured_content["data"]["count"] == 1
        check = await client.call_tool("self_check", {})
        assert check.structured_content["data"]["fake"]["ok"]


async def test_errors_redact_credentials(service):
    service.config.sites = {"rule34": {"api_key": "SECRET"}}

    async def error(*args, **kwargs):
        raise AuthError("bad api_key=SECRET")

    service.adapters["fake"].search = error
    result = await service.execute("search", site="fake", query="q")
    assert "SECRET" not in str(result)


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_real_stdio_process_from_different_working_directory(tmp_path, mode):
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
    async with Client(transport, timeout=15, mode=mode) as client:
        assert len(await client.list_tools()) == 6
        result = await client.call_tool(
            "search", {"site": "unknown", "query": "test"}, raise_on_error=False
        )
        assert result.is_error
        assert result.structured_content["error"]["type"] == "validation"


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_mcp_errors_preserve_structured_partial_results(service, mode):
    service.adapters["fake"].targets_count = 30
    service.adapters["fake"].posts *= 2
    async with Client(create_server(service=service), mode=mode) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert tools["search"].annotations.read_only_hint is True
        assert tools["download_post"].annotations.read_only_hint is False
        assert tools["download_post"].annotations.destructive_hint is True
        assert "ctx" not in tools["download_post"].input_schema["properties"]
        assert tools["download_post"].input_schema["properties"]["overwrite"]["default"] is False
        partial = await client.call_tool(
            "download_search", {"site": "fake", "query": "q"}, raise_on_error=False
        )
        assert partial.is_error
        data = partial.structured_content
        assert not data["success"] and data["data"]["total_files"] == 30
        assert data["error"]["type"] == "partial_download"
        assert json.loads(partial.content[0].text) == data
        invalid = await client.call_tool(
            "search", {"site": "unknown", "query": "q"}, raise_on_error=False
        )
        assert invalid.is_error and invalid.structured_content["error"]["type"] == "validation"
        wrong_type = await client.call_tool(
            "search", {"site": "fake", "query": "q", "limit": "2"}, raise_on_error=False
        )
        assert wrong_type.is_error


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_mcp_download_progress_is_monotonic(service, mode):
    events = []

    async def progress(current, total, message):
        events.append((current, total, message))

    async with Client(create_server(service=service), mode=mode) as client:
        await client.call_tool(
            "download_post", {"site": "fake", "post_id": "1"}, progress_handler=progress
        )
    assert len(events) >= 3
    assert all(left[0] < right[0] for left, right in zip(events, events[1:]))
    assert any("队列" in event[2] for event in events)
    assert events[-1][2] == "操作完成"


@pytest.mark.parametrize("error_type", ["auth", "quota", "not_found"])
async def test_batch_stops_only_on_site_rejection(service, error_type):
    seen = []
    service.adapters["fake"].posts = [Post(site="fake", id=str(i), url="u") for i in range(3)]

    async def download(post, targets, subdir=None, **kwargs):
        seen.append(post.id)
        return DownloadResult(
            post, "directory", [], "manifest", [{"type": error_type, "message": "test"}]
        )

    service.downloader.download = download
    result = await service.execute("download_search", site="fake", query="q")
    if error_type in {"auth", "quota"}:
        assert seen == ["0"]
        assert [p["id"] for p in result["data"]["skipped"]] == ["1", "2"]
        assert result["data"]["stop_reason"]["type"] == error_type
    else:
        assert seen == ["0", "1", "2"] and not result["data"]["skipped"]


@pytest.mark.parametrize("error", [AuthError, QuotaError])
async def test_batch_stops_when_target_resolution_rejects_site(service, error):
    seen = []
    service.adapters["fake"].posts *= 3

    async def targets(post):
        seen.append(post.id)
        raise error("test rejection")

    service.adapters["fake"].get_download_targets = targets
    result = await service.execute("download_search", site="fake", query="q")
    assert len(seen) == 1 and len(result["data"]["skipped"]) == 2


async def test_different_sites_download_independently_but_same_site_queues(service):
    started, release = asyncio.Event(), asyncio.Event()
    other = FakeAdapter()
    other.name = "other"
    service.adapters["other"] = other

    async def slow(post_id):
        started.set()
        await release.wait()
        return service.adapters["fake"].posts[0]

    service.adapters["fake"].get_post = slow
    task = asyncio.create_task(service.execute("download_post", site="fake", post_id="1"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        result = await service.execute("download_post", site="other", post_id="1", timeout=0.5)
        assert result["success"]
        queued = await service.execute("download_post", site="fake", post_id="1", timeout=0.02)
        assert queued["error"]["type"] == "timeout"
    finally:
        release.set()
        await task


async def test_modern_stdio_wire_discovery_and_error(tmp_path):
    config = tmp_path / "isolated.toml"
    config.write_text("[network]\nretries = 1\n", encoding="utf-8")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "media_mcp.server",
        cwd=tmp_path,
        env={**os.environ, "MEDIA_HUNTER_CONFIG": str(config), "MEDIA_HUNTER_TRANSPORT": "stdio"},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def request(request_id, method, **params):
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "media-tests", "version": "1"},
        }
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        await proc.stdin.drain()
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), 15)
            assert line, "MCP server exited without a response"
            response = json.loads(line)
            if response.get("id") == request_id:
                return response

    try:
        # 新版请求无需 initialize；直接验证 JSON-RPC wire 字段，而非 SDK 的内部模型。
        discovery = await request(1, "server/discover")
        assert discovery["result"]["resultType"] == "complete"
        listing = await request(2, "tools/list")
        assert listing["result"]["ttlMs"] == 60000
        assert listing["result"]["cacheScope"] == "private"
        assert len(listing["result"]["tools"]) == 6
        failed = await request(
            3, "tools/call", name="search", arguments={"site": "unknown", "query": "q"}
        )
        assert failed["result"]["resultType"] == "complete"
        assert failed["result"]["isError"] is True
        assert failed["result"]["structuredContent"]["error"]["type"] == "validation"
    finally:
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except TimeoutError:
            proc.kill()
            await proc.wait()


async def test_modern_streamable_http_requires_no_session(service):
    app = create_server(service=service).http_app(path="/mcp", json_response=True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            response = await client.post(
                "/mcp",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "MCP-Method": "tools/call",
                    "MCP-Name": "search",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "search",
                        "arguments": {"site": "fake", "query": "q"},
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                            "io.modelcontextprotocol/clientInfo": {"name": "tests", "version": "1"},
                        },
                    },
                },
            )
            assert response.status_code == 200
            assert "Mcp-Session-Id" not in response.headers
            result = response.json()["result"]
            assert result["resultType"] == "complete" and not result["isError"]
            assert result["structuredContent"]["data"]["count"] == 1
