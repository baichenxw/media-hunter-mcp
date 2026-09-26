"""搜索和下载的行为边界、工具发现与批量显式选择契约。"""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
import respx
from fastmcp import Client

from media_mcp.cli import parser
from media_mcp.config import Config
from media_mcp.models import AuthError, DownloadTarget, NotFoundError, Post
from media_mcp.server import create_server
from media_mcp.service import MediaService


class SelectedAdapter:
    name = "e621"

    def __init__(self):
        self.posts = {
            str(i): Post(
                site=self.name, id=str(i), url=f"https://e621.net/posts/{i}", title=f"post {i}"
            )
            for i in range(1, 4)
        }
        self.search_calls = 0
        self.detail_calls = []

    async def search(self, query, **kwargs):
        self.search_calls += 1
        return list(self.posts.values())

    async def get_post(self, post_id):
        self.detail_calls.append(post_id)
        if post_id not in self.posts:
            raise NotFoundError("作品不存在")
        return replace(self.posts[post_id])

    async def get_download_targets(self, post):
        return [DownloadTarget(f"https://cdn.example/{post.id}.png", f"{post.id}.png")]

    def parse_url(self, url):
        return next((p.id for p in self.posts.values() if p.url == url), None)

    async def check(self):
        return {"ok": True, "detail": "test"}


@pytest.fixture
async def service(tmp_path):
    service = MediaService(
        Config(download_root=tmp_path / "downloads", sites={"e621": {"request_interval": 0}})
    )
    service.adapters = {"e621": SelectedAdapter()}
    yield service
    await service.close()


async def test_search_and_details_never_resolve_or_write_media(service):
    async def forbidden(*args, **kwargs):
        pytest.fail("只读工具触发了媒体解析或下载")

    service.adapters["e621"].get_download_targets = forbidden
    service.downloader.download = forbidden
    assert (await service.execute("search", site="e621", query="landscape"))["success"]
    assert (await service.execute("get_post", site="e621", post_id="1"))["success"]
    assert not service.config.download_root.exists()


@pytest.mark.parametrize(
    "operation,args",
    [
        ("download_post", {"post_id": "2"}),
        ("download_posts", {"post_ids": ["2"]}),
        ("download_url", {"url": "https://e621.net/posts/2"}),
    ],
)
async def test_every_download_only_fetches_selected_ids_and_never_searches(
    service, operation, args
):
    async def forbidden(*args, **kwargs):
        pytest.fail("下载工具调用了搜索接口")

    adapter = service.adapters["e621"]
    adapter.search = forbidden
    with respx.mock:
        media = respx.get("https://cdn.example/2.png").respond(200, content=b"image")
        if operation != "download_url":
            args["site"] = "e621"
        result = await service.execute(operation, **args)
        assert result["success"]
        assert adapter.detail_calls == ["2"] and media.call_count == 1
        assert len(list(service.config.download_root.rglob("*.png"))) == 1


@pytest.mark.parametrize("ids", [[], "1", ["1", 2], ["1", "bad"], [" "], [None], ["1"] * 51])
async def test_batch_validates_entire_list_before_any_io(service, ids):
    with respx.mock:
        result = await service.execute("download_posts", site="e621", post_ids=ids)
    assert result["error"]["type"] == "validation"
    assert not service.adapters["e621"].detail_calls
    assert not service.config.download_root.exists()


async def test_batch_deduplicates_in_order_and_reuses_downloads(service):
    with respx.mock:
        first = respx.get("https://cdn.example/1.png").respond(200, content=b"first")
        second = respx.get("https://cdn.example/2.png").respond(200, content=b"second")
        for repeat in range(2):
            result = await service.execute("download_posts", site="e621", post_ids=["2", "1", "2"])
            data = result["data"]
            assert result["success"]
            assert data["requested_ids"] == ["2", "1"]
            assert [row["id"] for row in data["downloaded"]] == ["2", "1"]
            assert data["attempted_files"] == data["total_files"] == 2
            assert data["reused_files"] == repeat * 2
        assert first.call_count == second.call_count == 1
        assert service.adapters["e621"].search_calls == 0


@pytest.mark.parametrize("failure", [AuthError, NotFoundError])
async def test_batch_metadata_errors_stop_or_continue_as_documented(service, failure):
    adapter = service.adapters["e621"]
    lookup = adapter.get_post

    async def get_post(post_id):
        if post_id == "2":
            adapter.detail_calls.append(post_id)
            raise failure("test")
        return await lookup(post_id)

    adapter.get_post = get_post
    with respx.mock:
        respx.get("https://cdn.example/1.png").respond(200, content=b"first")
        if failure is NotFoundError:
            respx.get("https://cdn.example/3.png").respond(200, content=b"third")
        result = await service.execute("download_posts", site="e621", post_ids=["1", "2", "3"])
    data = result["data"]
    assert not result["success"] and data["errors"][0]["id"] == "2"
    if failure is AuthError:
        assert adapter.detail_calls == ["1", "2"]
        assert data["skipped"][0]["id"] == "3" and data["stop_reason"]["type"] == "auth"
    else:
        assert adapter.detail_calls == ["1", "2", "3"]
        assert [row["id"] for row in data["downloaded"]] == ["1", "3"] and not data["skipped"]


async def test_batch_timeout_preserves_finished_files_and_reports_pending_ids(service):
    adapter = service.adapters["e621"]
    lookup = adapter.get_post

    async def get_post(post_id):
        if post_id == "2":
            await asyncio.sleep(10)
        return await lookup(post_id)

    adapter.get_post = get_post
    with respx.mock:
        respx.get("https://cdn.example/1.png").respond(200, content=b"first")
        result = await service.execute(
            "download_posts", site="e621", post_ids=["1", "2", "3"], timeout=1
        )
    data = result["data"]
    assert result["error"]["type"] == "partial_download"
    assert data["total_files"] == 1 and data["errors"][0]["id"] == "2"
    assert data["errors"][0]["type"] == "timeout"
    assert data["skipped"] == [{"id": "3", "reason": "批量任务超时，尚未处理"}]
    assert Path(data["downloaded"][0]["files"][0]["path"]).read_bytes() == b"first"


async def test_legacy_combined_operation_is_not_callable(service):
    result = await service.execute("download_search", site="e621", query="landscape")
    assert result["error"]["type"] == "validation"
    assert service.adapters["e621"].search_calls == 0
    assert not service.config.download_root.exists()


def test_cli_replaces_combined_search_with_selected_ids():
    args = parser().parse_args(["download-posts", "pixiv", "123", "456", "--timeout", "90"])
    assert args.post_ids == ["123", "456"]
    assert not hasattr(args, "query")
    with pytest.raises(SystemExit):
        parser().parse_args(["download-search", "pixiv", "landscape"])


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_mcp_discovery_is_unambiguous_and_search_to_download_flow(service, mode):
    with respx.mock:
        respx.get("https://cdn.example/2.png").respond(200, content=b"chosen")
        async with Client(create_server(service=service), mode=mode) as client:
            tools = {t.name: t for t in await client.list_tools()}
            assert set(tools) == {
                "media_hunter_" + name
                for name in (
                    "search",
                    "get_post",
                    "preview",
                    "download",
                    "download_url",
                    "self_check",
                )
            }
            for name, tool in tools.items():
                props = tool.input_schema["properties"]
                assert tool.description and "返回" in tool.description
                assert all(value.get("description") for value in props.values())
                assert "ctx" not in props
                if "site" in props:
                    assert props["site"]["enum"] == ["e621", "rule34", "ehentai", "pixiv"]
                if "download" in name:
                    assert not tool.annotations.read_only_hint
                    assert not {"query", "rating", "page", "limit", "min_score"} & props.keys()
                else:
                    assert tool.annotations.read_only_hint
            result = await client.call_tool(
                "media_hunter_search", {"site": "e621", "query": "landscape"}
            )
            assert not service.config.download_root.exists()
            selected = result.structured_content["data"]["posts"][1]
            assert selected["id"] == "2"
            downloaded = await client.call_tool(
                "media_hunter_download",
                {"site": selected["site"], "post_ids": [selected["id"]]},
            )
            assert downloaded.structured_content["data"]["total_files"] == 1
            assert service.adapters["e621"].search_calls == 1
            assert service.adapters["e621"].detail_calls == ["2"]


async def test_mcp_download_rejects_search_parameters_before_side_effects(service):
    async with Client(create_server(service=service)) as client:
        result = await client.call_tool(
            "media_hunter_download",
            {"site": "e621", "post_ids": ["1"], "query": "landscape"},
            raise_on_error=False,
        )
        assert result.is_error
    assert not service.adapters["e621"].detail_calls
    assert not service.config.download_root.exists()


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
@pytest.mark.parametrize("post_ids", [["1"], ["1", "2"]])
async def test_mcp_unified_download_shape_and_no_search(service, mode, post_ids):
    async def forbidden(*args, **kwargs):
        pytest.fail("下载工具调用了搜索接口")

    adapter = service.adapters["e621"]
    adapter.search = forbidden
    with respx.mock:
        for post_id in post_ids:
            respx.get(f"https://cdn.example/{post_id}.png").respond(200, content=b"selected")
        async with Client(create_server(service=service), mode=mode) as client:
            result = await client.call_tool(
                "media_hunter_download", {"site": "e621", "post_ids": post_ids}
            )
    data = result.structured_content["data"]
    assert result.structured_content["success"] and not result.is_error
    assert data["requested_ids"] == post_ids
    assert [row["id"] for row in data["downloaded"]] == post_ids
    assert all(row["complete"] and len(row["files"]) == 1 for row in data["downloaded"])
    assert data["total_files"] == len(post_ids) and adapter.detail_calls == post_ids
    assert not data["errors"] and not data["skipped"] and data["stop_reason"] is None


@pytest.mark.parametrize(
    "options",
    [
        {"post_id": "1"},
        {"post_ids": "1"},
        {"post_ids": []},
        {"post_ids": ["1", 2]},
        {"post_ids": ["1"] * 51},
    ],
)
async def test_mcp_unified_download_rejects_invalid_list_before_io(service, options):
    async with Client(create_server(service=service)) as client:
        result = await client.call_tool(
            "media_hunter_download", {"site": "e621", **options}, raise_on_error=False
        )
    assert result.is_error
    assert not service.adapters["e621"].detail_calls
    assert not service.config.download_root.exists()
