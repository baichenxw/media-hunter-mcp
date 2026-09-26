"""通过真实 MCP Client 验证图片块、元数据映射和只读预览的边界。"""

import asyncio
import base64
import json
from io import BytesIO

import pytest
import respx
from fastmcp import Client
from PIL import Image

from media_mcp.config import Config
from media_mcp.models import Post
from media_mcp.server import create_server
from media_mcp.service import MediaService
from media_mcp.sites.ehentai import EHentaiAdapter


def source_image(color):
    output = BytesIO()
    Image.new("RGBA", (1600, 900), color).save(output, format="PNG")
    return output.getvalue()


class PreviewAdapter:
    name = "e621"

    def __init__(self):
        self.detail_calls = []

    async def get_post(self, post_id):
        self.detail_calls.append(post_id)
        return Post(
            site=self.name,
            id=post_id,
            url=f"https://e621.net/posts/{post_id}",
            preview_url=f"https://static1.e621.net/preview/{post_id}.png",
        )

    async def search(self, *args, **kwargs):
        pytest.fail("预览调用了搜索工具")

    async def get_download_targets(self, *args, **kwargs):
        pytest.fail("预览调用了媒体下载解析")


@pytest.fixture
async def service(tmp_path):
    service = MediaService(
        Config(download_root=tmp_path / "downloads", sites={"e621": {"request_interval": 0}})
    )
    service.adapters = {"e621": PreviewAdapter()}

    async def forbidden(*args, **kwargs):
        pytest.fail("预览调用了下载器")

    service.downloader.download = forbidden
    yield service
    await service.close()
    assert list(tmp_path.iterdir()) == []


def inspect_result(result):
    """让真实客户端收到的每个图片块再次解码，而不是只检查类型或长度。"""
    envelope = result.structured_content
    assert result.content[0].type == "text"
    assert json.loads(result.content[0].text) == envelope
    encoded_metadata = json.dumps(envelope, ensure_ascii=False)
    image_blocks = [block for block in result.content if block.type == "image"]
    previews = envelope["data"]["previews"]
    assert len(image_blocks) == len(previews) == envelope["data"]["count"]
    colors = []
    total = 0
    for index, (metadata, block) in enumerate(zip(previews, image_blocks, strict=True), 1):
        assert metadata["image_index"] == index
        assert block.mime_type == metadata["mime_type"] == "image/jpeg"
        assert block.data not in encoded_metadata
        assert all(block.data not in item.text for item in result.content if item.type == "text")
        raw = base64.b64decode(block.data, validate=True)
        assert len(raw) == metadata["size_bytes"] <= 96 * 1024
        with Image.open(BytesIO(raw)) as decoded:
            decoded.load()
            assert decoded.format == "JPEG" and decoded.mode == "RGB"
            assert decoded.size == (metadata["width"], metadata["height"])
            assert max(decoded.size) <= 640
            assert not decoded.getexif()
            colors.append(decoded.getpixel((decoded.width // 2, decoded.height // 2)))
        total += len(raw)
    assert envelope["data"]["total_bytes"] == total
    return envelope["data"], colors


async def test_stalled_progress_does_not_prevent_preview_timeout(service):
    calls = 0

    async def stalled_progress(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    async with asyncio.timeout(1):
        result, images = await service.preview_posts(
            "e621", ["1"], timeout=0.02, progress=stalled_progress
        )
    assert not result["success"] and not images
    assert result["data"]["errors"][0]["type"] == "timeout"
    assert calls == 1


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_mcp_preview_images_are_small_ordered_and_never_written(service, mode):
    with respx.mock:
        red = respx.get("https://static1.e621.net/preview/1.png").respond(
            200, content=source_image("red"), headers={"content-type": "image/png"}
        )
        blue = respx.get("https://static1.e621.net/preview/2.png").respond(
            200, content=source_image("blue"), headers={"content-type": "image/png"}
        )
        async with Client(create_server(service=service), mode=mode) as client:
            result = await client.call_tool(
                "media_hunter_preview", {"site": "e621", "post_ids": ["2", "1", "2"]}
            )
        assert red.call_count == blue.call_count == 1
    data, colors = inspect_result(result)
    assert result.structured_content["success"] and not result.is_error
    assert data["requested_ids"] == ["2", "1"]
    assert [item["id"] for item in data["previews"]] == ["2", "1"]
    assert service.adapters["e621"].detail_calls == ["2", "1"]
    assert all(item["scope"] == "thumbnail" for item in data["previews"])
    assert colors[0][2] > 240 and colors[0][0] < 10
    assert colors[1][0] > 240 and colors[1][2] < 10
    assert not data["errors"] and not data["skipped"] and data["complete"]


@pytest.mark.parametrize(
    "post_ids",
    [[], "1", ["1", 2], ["1", "bad"], ["1", "https://e621.net/posts/2"], ["1"] * 5],
)
async def test_mcp_preview_rejects_entire_invalid_batch_before_io(service, post_ids):
    with respx.mock as router:
        async with Client(create_server(service=service)) as client:
            result = await client.call_tool(
                "media_hunter_preview",
                {"site": "e621", "post_ids": post_ids},
                raise_on_error=False,
            )
        assert len(router.calls) == 0
    assert result.is_error
    assert not any(block.type == "image" for block in result.content)
    assert not service.adapters["e621"].detail_calls


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_mcp_preview_failed_image_preserves_other_images_and_mapping(service, mode):
    with respx.mock:
        respx.get("https://static1.e621.net/preview/1.png").respond(
            200, content=source_image("red")
        )
        respx.get("https://static1.e621.net/preview/2.png").respond(404)
        respx.get("https://static1.e621.net/preview/3.png").respond(
            200, content=source_image("blue")
        )
        async with Client(create_server(service=service), mode=mode) as client:
            result = await client.call_tool(
                "media_hunter_preview",
                {"site": "e621", "post_ids": ["1", "2", "3"]},
                raise_on_error=False,
            )
    data, colors = inspect_result(result)
    assert result.is_error and not result.structured_content["success"]
    assert result.structured_content["error"]["type"] == "partial_preview"
    assert [(item["id"], item["image_index"]) for item in data["previews"]] == [
        ("1", 1),
        ("3", 2),
    ]
    assert data["errors"][0]["id"] == "2" and data["errors"][0]["type"] == "not_found"
    assert not data["skipped"] and not data["complete"]
    assert colors[0][0] > 240 and colors[1][2] > 240


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_mcp_preview_timeout_keeps_completed_image_and_identifies_pending_ids(service, mode):
    adapter = service.adapters["e621"]
    lookup = adapter.get_post
    cancelled = asyncio.Event()

    async def slow_second(post_id):
        if post_id == "2":
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        return await lookup(post_id)

    adapter.get_post = slow_second
    with respx.mock:
        respx.get("https://static1.e621.net/preview/1.png").respond(
            200, content=source_image("red")
        )
        async with Client(create_server(service=service), mode=mode) as client:
            result = await client.call_tool(
                "media_hunter_preview",
                {"site": "e621", "post_ids": ["1", "2", "3"], "timeout": 1.0},
                raise_on_error=False,
            )
    data, _ = inspect_result(result)
    assert result.is_error and cancelled.is_set()
    assert [item["id"] for item in data["previews"]] == ["1"]
    assert data["errors"][0]["id"] == "2" and data["errors"][0]["type"] == "timeout"
    assert [item["id"] for item in data["skipped"]] == ["3"]
    assert adapter.detail_calls == ["1"] and not data["complete"]


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_mcp_preview_ehentai_choices_are_isolated_and_covers_are_labelled(
    tmp_path, monkeypatch, mode
):
    config = Config(
        download_root=tmp_path / "downloads",
        sites={"ehentai": {"request_interval": 0, "use_exhentai": True}},
    )
    service = MediaService(config)
    choices = []

    async def get_post(adapter, post_id):
        choices.append((adapter.use_exhentai, adapter.original))
        await asyncio.sleep(0)
        site_name = "private" if adapter.use_exhentai else "public"
        return Post(
            site="ehentai",
            id=post_id,
            url=f"https://{'exhentai.org' if adapter.use_exhentai else 'e-hentai.org'}/g/{post_id}/",
            preview_url=f"https://ehgt.org/{site_name}.png",
            page_count=100,
            extra={"use_exhentai": adapter.use_exhentai},
        )

    async def forbidden(*args, **kwargs):
        pytest.fail("画廊预览请求了解析整部作品或下载原图")

    monkeypatch.setattr(EHentaiAdapter, "get_post", get_post)
    monkeypatch.setattr(EHentaiAdapter, "get_download_targets", forbidden)
    service.downloader.download = forbidden
    try:
        with respx.mock:
            respx.get("https://ehgt.org/public.png").respond(200, content=source_image("red"))
            respx.get("https://ehgt.org/private.png").respond(200, content=source_image("blue"))
            async with Client(create_server(service=service), mode=mode) as client:
                public, private = await asyncio.gather(
                    client.call_tool(
                        "media_hunter_preview",
                        {"site": "ehentai", "post_ids": ["123/abc"], "use_exhentai": False},
                    ),
                    client.call_tool(
                        "media_hunter_preview", {"site": "ehentai", "post_ids": ["123/abc"]}
                    ),
                )
        for result, expected_choice in [(public, False), (private, True)]:
            data, _ = inspect_result(result)
            assert data["previews"][0]["scope"] == "cover"
            assert data["previews"][0]["page_count"] == 100
            assert data["previews"][0]["use_exhentai"] is expected_choice
        assert sorted(choices) == [(False, False), (True, False)]
        assert service.adapters["ehentai"].use_exhentai is True
        assert config.site_get("ehentai", "use_exhentai") is True
        assert list(tmp_path.iterdir()) == []
    finally:
        await service.close()
