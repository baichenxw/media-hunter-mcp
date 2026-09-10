"""真实适配器→HTTP→下载引擎的离线集成测试。"""

import json
from pathlib import Path

import pytest
import respx

from media_mcp.config import Config, NetworkConfig
from media_mcp.service import MediaService


@pytest.fixture
async def service(tmp_path):
    config = Config(
        download_root=tmp_path,
        network=NetworkConfig(retries=1),
        sites={
            "e621": {"request_interval": 0},
            "rule34": {"request_interval": 0, "user_id": "1", "api_key": "test-secret"},
            "ehentai": {"request_interval": 0},
            "pixiv": {"request_interval": 0, "refresh_token": "test-token"},
        },
    )
    service = MediaService(config)
    yield service
    await service.close()


async def test_rule34_url_download(service):
    with respx.mock:
        respx.get("https://api.rule34.xxx/index.php").respond(
            200,
            json=[
                {
                    "id": 12,
                    "tags": "landscape",
                    "rating": "safe",
                    "file_url": "https://cdn.example/12.png",
                    "score": 1,
                }
            ],
        )
        media = respx.get("https://cdn.example/12.png").respond(
            200, content=b"test-png", headers={"content-type": "image/png"}
        )
        result = await service.execute(
            "download_url", url="https://rule34.xxx/index.php?page=post&s=view&id=12"
        )
        assert result["success"]
        assert Path(result["data"]["files"][0]["path"]).read_bytes() == b"test-png"
        assert "api_key" not in str(media.calls.last.request.url)
        assert "test-secret" not in str(media.calls.last.request.headers)


async def test_ehentai_partial_gallery_keeps_successful_pages(service):
    with respx.mock:
        respx.post("https://api.e-hentai.org/api.php").respond(
            200,
            json={"gmetadata": [{"gid": 12, "token": "abc", "title": "gallery", "filecount": "2"}]},
        )
        respx.get("https://e-hentai.org/g/12/abc/").respond(
            200, text='<a href="/s/aa/12-1">1</a><a href="/s/bb/12-2">2</a>'
        )
        respx.get("https://e-hentai.org/s/aa/12-1").respond(
            200, text='<img id="img" src="https://cdn.example/a.webp">'
        )
        respx.get("https://e-hentai.org/s/bb/12-2").respond(404)
        respx.get("https://cdn.example/a.webp").respond(
            200, content=b"webp", headers={"content-type": "image/webp"}
        )
        result = await service.execute("download_post", site="ehentai", post_id="12/abc")
        assert not result["success"]
        assert result["error"]["type"] == "partial_download"
        assert len(result["data"]["files"]) == 1
        assert Path(result["data"]["files"][0]["path"]).name == "001.webp"
        manifest = json.loads(Path(result["data"]["sidecar_path"]).read_text(encoding="utf-8"))
        assert manifest["status"] == "partial" and manifest["errors"][0]["page"] == 2


async def test_pixiv_single_page_oauth_and_file_download(service):
    with respx.mock:
        respx.post("https://oauth.secure.pixiv.net/auth/token").respond(
            200, json={"access_token": "access", "expires_in": 3600}
        )
        respx.get("https://app-api.pixiv.net/v1/illust/detail").respond(
            200,
            json={
                "illust": {
                    "id": 12,
                    "meta_single_page": {"original_image_url": "https://i.pximg.net/a.png"},
                }
            },
        )
        media = respx.get("https://i.pximg.net/a.png").respond(
            200, content=b"png", headers={"content-type": "image/png"}
        )
        result = await service.execute("download_url", url="https://www.pixiv.net/artworks/12")
        assert result["success"] and len(result["data"]["files"]) == 1
        assert media.calls.last.request.headers["Referer"] == "https://pixiv.net"
        assert "Authorization" not in media.calls.last.request.headers


@pytest.mark.parametrize(
    "url",
    [
        "https://evile621.net/posts/12",
        "https://rule34.xxx.evil.example/index.php?page=post&s=view&id=12",
        "https://evil.example/?u=https://e-hentai.org/g/12/abc/",
        "file:///https://pixiv.net/artworks/12",
    ],
)
async def test_forged_site_urls_rejected_without_network(service, url):
    with respx.mock:
        result = await service.execute("download_url", url=url)
        assert result["error"]["type"] == "validation"
