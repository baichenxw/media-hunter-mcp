import asyncio
import json
import struct
import zipfile
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import respx

from media_mcp.config import Config, NetworkConfig
from media_mcp.downloader import Downloader, find_ffmpeg, safe_filename, sanitize
from media_mcp.models import DownloadTarget, Post, ValidationError
from media_mcp.network import Network


class FakeNetwork:
    def __init__(self, content=b"image-bytes"):
        self.content = content
        self.urls = []

    @asynccontextmanager
    async def stream(self, site, method, url, **kwargs):
        self.urls.append(url)
        yield httpx.Response(200, content=self.content)


def test_sanitize():
    assert sanitize("a/b:c*d") == "a_b_c_d"
    assert sanitize("") == "untitled"
    assert sanitize("  name.  ") == "name"
    assert sanitize("CON.txt") != "CON.txt"
    assert len(sanitize("x" * 100)) == 60
    assert safe_filename("x" * 200 + ".png").endswith(".png")


async def test_pixiv_files_and_manifest(tmp_path):
    downloader = Downloader(Config(download_root=tmp_path), FakeNetwork())
    post = Post(site="pixiv", id="100", url="u", artist=["作者A"], extra={"user_id": 7})
    targets = [DownloadTarget(f"https://x/{i}.jpg", f"100_p{i}.jpg", page=i) for i in range(2)]
    result = await downloader.download(post, targets)
    assert Path(result.directory) == tmp_path / "pixiv" / "作者A_7"
    assert Path(result.files[0].path).name == "100_p0.jpg"
    data = json.loads(Path(result.sidecar_path).read_text(encoding="utf-8"))
    assert data["status"] == "complete" and len(data["files"]) == 2


async def test_gallery_subdir_does_not_overwrite(tmp_path):
    downloader = Downloader(Config(download_root=tmp_path), FakeNetwork())
    target = [DownloadTarget("https://x/a.jpg", "001.jpg")]
    paths = []
    for gid in ["123", "456"]:
        result = await downloader.download(
            Post(site="ehentai", id=gid + "/abc", title="same", url="u"), target, "collection"
        )
        paths.append(result.files[0].path)
    assert paths[0] != paths[1]
    assert all(Path(path).is_file() for path in paths)


async def test_concurrency_and_partial_failure(tmp_path, monkeypatch):
    downloader = Downloader(
        Config(download_root=tmp_path, sites={"e621": {"download_concurrency": 2}}), FakeNetwork()
    )
    active = peak = 0

    async def download(site, target, path):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        try:
            await asyncio.sleep(0.01)
            if target.page == 2:
                raise OSError("disk error")
            path.write_bytes(b"ok")
        finally:
            active -= 1

    monkeypatch.setattr(downloader, "_download_one", download)
    result = await downloader.download(
        Post(site="e621", id="1", url="u"),
        [DownloadTarget("u", f"{i}.jpg", page=i) for i in range(6)],
    )
    assert peak == 2 and active == 0
    assert [f.page for f in result.files] == [0, 1, 3, 4, 5]
    assert len(result.errors) == 1 and not result.to_dict()["complete"]


class InterruptedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"first"
        raise httpx.ReadError("disconnected")


async def test_stream_failure_preserves_existing_file(tmp_path):
    config = Config(
        download_root=tmp_path,
        network=NetworkConfig(retries=1),
        sites={"e621": {"request_interval": 0}},
    )
    network = Network(config)
    downloader = Downloader(config, network)
    post = Post(site="e621", id="1", url="u")
    directory = downloader.directory_for(post)
    directory.mkdir(parents=True)
    (directory / "1.jpg").write_bytes(b"old complete file")
    with respx.mock:
        respx.get("https://x/1.jpg").respond(200, stream=InterruptedStream())
        result = await downloader.download(post, [DownloadTarget("https://x/1.jpg", "1.jpg")])
    assert result.errors and (directory / "1.jpg").read_bytes() == b"old complete file"
    assert not list(directory.glob("*.part"))
    await network.close()


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield b"partial"
        await asyncio.sleep(20)

    async def aclose(self):
        self.closed = True


async def test_cancel_cleans_streams_and_temp_files(tmp_path):
    config = Config(
        download_root=tmp_path,
        network=NetworkConfig(retries=1),
        sites={"e621": {"request_interval": 0}},
    )
    network = Network(config)
    downloader = Downloader(config, network)
    stream = BlockingStream()
    post = Post(site="e621", id="1", url="u")
    with respx.mock:
        respx.get("https://x/a.jpg").respond(200, stream=stream)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await downloader.download(post, [DownloadTarget("https://x/a.jpg", "a.jpg")])
    directory = downloader.directory_for(post)
    assert not (directory / "a.jpg").exists()
    assert not list(directory.glob("*.part")) and stream.closed
    assert json.loads((directory / "1.json").read_text(encoding="utf-8"))["status"] == "cancelled"
    await network.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>challenge</html>"),
        httpx.Response(200, content=b"", headers={"content-type": "image/jpeg"}),
        httpx.Response(200, content=b"x", headers={"content-length": "20"}),
    ],
)
async def test_reject_invalid_download(tmp_path, response):
    config = Config(
        download_root=tmp_path,
        network=NetworkConfig(retries=1),
        sites={"e621": {"request_interval": 0}},
    )
    network = Network(config)
    with respx.mock:
        respx.get("https://x/a.jpg").mock(return_value=response)
        result = await Downloader(config, network).download(
            Post(site="e621", id="1", url="u"), [DownloadTarget("https://x/a.jpg", "a.jpg")]
        )
    assert result.errors and not result.files
    assert not list(tmp_path.rglob("*.part"))
    await network.close()


def png(color):
    def chunk(kind, data):
        return (
            struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + bytes(color) * 4) * 4))
        + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize("fmt", ["gif", "mp4"])
async def test_real_ffmpeg_ugoira(tmp_path, fmt):
    if not find_ffmpeg(Config()):
        pytest.skip("ffmpeg not installed")
    zip_path = tmp_path / "778_ugoira.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("000.png", png((255, 0, 0)))
        archive.writestr("001.png", png((0, 255, 0)))
    downloader = Downloader(
        Config(download_root=tmp_path, sites={"pixiv": {"ugoira_format": fmt}}), FakeNetwork()
    )
    out = await downloader._compose_ugoira(
        zip_path, {"frames": [{"file": "001.png", "delay": 120}, {"file": "000.png", "delay": 100}]}
    )
    assert out.suffix == "." + fmt and out.stat().st_size > 0
    assert not zip_path.exists() and not list(tmp_path.glob("ugoira-*"))


async def test_reject_zip_traversal(tmp_path):
    if not find_ffmpeg(Config()):
        pytest.skip("ffmpeg not installed")
    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("../escape.png", b"bad")
    with pytest.raises(ValidationError):
        await Downloader(Config(), FakeNetwork())._compose_ugoira(zip_path, {})
    assert zip_path.exists() and not (tmp_path.parent / "escape.png").exists()
