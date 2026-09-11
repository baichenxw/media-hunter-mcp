import asyncio
import json
import re
import struct
import subprocess
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

    async def download(site, target, path, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        try:
            await asyncio.sleep(0.01)
            if target.page == 2:
                raise OSError("disk error")
            path.write_bytes(b"ok")
            return None, path
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


@pytest.mark.parametrize("attempts", [2, 3])
async def test_download_shares_connect_and_body_retry_budget(tmp_path, monkeypatch, attempts):
    config = Config(
        download_root=tmp_path,
        network=NetworkConfig(retries=attempts),
        sites={"e621": {"request_interval": 0}},
    )
    network = Network(config)
    monkeypatch.setattr(Network, "_backoff", staticmethod(lambda *args: 0))
    sequence = []
    for _ in range(attempts):
        sequence.extend([httpx.ConnectError("failed")] * (attempts - 1))
        sequence.append(httpx.Response(200, stream=InterruptedStream()))
    try:
        with respx.mock:
            route = respx.get("https://x/a.jpg").mock(side_effect=sequence)
            result = await Downloader(config, network).download(
                Post(site="e621", id="1", url="u"), [DownloadTarget("https://x/a.jpg", "a.jpg")]
            )
        assert route.call_count == attempts
        assert result.errors[0]["type"] == "network"
        assert not list(tmp_path.rglob("*.part"))
    finally:
        await network.close()


async def test_status_and_body_retry_can_recover(tmp_path, monkeypatch):
    config = Config(
        download_root=tmp_path,
        network=NetworkConfig(retries=3),
        sites={"e621": {"request_interval": 0}},
    )
    network = Network(config)
    monkeypatch.setattr(Network, "_backoff", staticmethod(lambda *args: 0))
    try:
        with respx.mock:
            route = respx.get("https://x/a.jpg").mock(
                side_effect=[
                    httpx.Response(503),
                    httpx.Response(200, stream=InterruptedStream()),
                    httpx.Response(200, content=b"complete"),
                ]
            )
            result = await Downloader(config, network).download(
                Post(site="e621", id="1", url="u"), [DownloadTarget("https://x/a.jpg", "a.jpg")]
            )
        assert not result.errors and route.call_count == 3
        assert Path(result.files[0].path).read_bytes() == b"complete"
    finally:
        await network.close()


async def test_resume_checks_hash_and_overwrite_bypasses_cache(tmp_path):
    network = FakeNetwork(b"original")
    downloader = Downloader(Config(download_root=tmp_path), network)
    post = Post(site="pixiv", id="1", url="u")
    targets = [DownloadTarget(f"https://x/{i}.png", f"1_{i}.png", page=i) for i in range(2)]
    first = await downloader.download(post, targets)
    resumed = await downloader.download(post, targets)
    assert len(network.urls) == 2 and resumed.to_dict()["reused_files"] == 2
    # 同样长度的损坏也必须被发现，不能只比较文件大小。
    Path(first.files[0].path).write_bytes(b"modified")
    repaired = await downloader.download(post, targets)
    assert len(network.urls) == 3 and repaired.to_dict()["reused_files"] == 1
    assert Path(first.files[0].path).read_bytes() == b"original"
    replaced = await downloader.download(post, targets, overwrite=True)
    assert len(network.urls) == 5 and replaced.to_dict()["reused_files"] == 0


async def test_resume_lazy_gallery_does_not_resolve_existing_page(tmp_path):
    network = FakeNetwork()
    downloader = Downloader(Config(download_root=tmp_path), network)
    resolved = []

    async def resolve(target):
        resolved.append(target.url)
        return DownloadTarget("https://cdn.example/a.webp", "001.webp", page=1)

    post = Post(site="ehentai", id="1/abc", url="u")
    targets = [DownloadTarget("https://e-hentai.org/s/aa/1-1", "001.jpg", page=1, resolve=resolve)]
    await downloader.download(post, targets)
    result = await downloader.download(post, targets)
    assert len(resolved) == len(network.urls) == 1
    assert result.files[0].reused and result.files[0].path.endswith("001.webp")


async def test_cancel_preserves_manifest_for_next_resume(tmp_path, monkeypatch):
    network = FakeNetwork()
    downloader = Downloader(Config(download_root=tmp_path), network)
    post = Post(site="e621", id="1", url="u")
    targets = [DownloadTarget("https://x/a.png", "a.png")]
    first = await downloader.download(post, targets)

    async def slow(*args):
        await asyncio.sleep(10)

    with monkeypatch.context() as patch:
        patch.setattr(downloader, "_reuse", slow)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.02):
                await downloader.download(post, targets)
    manifest = json.loads(Path(first.sidecar_path).read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled" and len(manifest["files"]) == 1
    result = await downloader.download(post, targets)
    assert result.files[0].reused and len(network.urls) == 1


@pytest.mark.parametrize("fmt", ["gif", "mp4"])
@pytest.mark.parametrize("delays", [[20, 20, 20, 20], [120, 100], [100], [17, 23, 41]])
async def test_ugoira_preserves_frame_timing(tmp_path, fmt, delays):
    ffmpeg = find_ffmpeg(Config())
    if not ffmpeg:
        pytest.skip("ffmpeg not installed")
    zip_path = tmp_path / "timing.zip"
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    with zipfile.ZipFile(zip_path, "w") as archive:
        for i in range(len(delays)):
            archive.writestr(f"{i}.png", png(colors[i]))
    downloader = Downloader(Config(sites={"pixiv": {"ugoira_format": fmt}}), FakeNetwork())
    out = await downloader._compose_ugoira(
        zip_path,
        {"frames": [{"file": f"{i}.png", "delay": delay} for i, delay in enumerate(delays)]},
    )
    if fmt == "gif":
        # 直接读取 GIF Graphic Control Extension 中的延时，避免播放器的最小延时策略干扰。
        raw = re.findall(rb"\x21\xf9\x04.([\s\S]{2}).\x00", out.read_bytes())
        actual = [struct.unpack("<H", value)[0] * 10 for value in raw]
        assert actual == [max(10, (delay + 5) // 10 * 10) for delay in delays]
    else:
        probe = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-i",
                str(out),
                "-vf",
                "showinfo",
                "-fps_mode",
                "passthrough",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        pts = [float(value) for value in re.findall(r"\bpts_time:([\d.]+)", probe.stderr)]
        durations = [
            float(value) for value in re.findall(r"\bduration_time:([\d.]+)", probe.stderr)
        ]
        assert pts[: len(delays)] == pytest.approx(
            [sum(delays[:i]) / 1000 for i in range(len(delays))]
        )
        assert pts[-1] + durations[-1] == pytest.approx(sum(delays) / 1000, abs=0.001)


async def test_resume_partial_download_requests_only_failed_file(tmp_path):
    config = Config(
        download_root=tmp_path,
        network=NetworkConfig(retries=1),
        sites={"pixiv": {"request_interval": 0}},
    )
    network = Network(config)
    downloader = Downloader(config, network)
    post = Post(site="pixiv", id="1", url="u")
    targets = [DownloadTarget(f"https://x/{i}.png", f"1_{i}.png", page=i) for i in range(2)]
    try:
        with respx.mock:
            good = respx.get("https://x/0.png").respond(200, content=b"good")
            failed = respx.get("https://x/1.png").mock(
                side_effect=[
                    httpx.Response(200, stream=InterruptedStream()),
                    httpx.Response(200, content=b"recovered"),
                ]
            )
            first = await downloader.download(post, targets)
            assert len(first.files) == len(first.errors) == 1
            second = await downloader.download(post, targets)
            assert not second.errors and second.to_dict()["reused_files"] == 1
            assert good.call_count == 1 and failed.call_count == 2
            manifest = json.loads(Path(second.sidecar_path).read_text(encoding="utf-8"))
            assert manifest["status"] == "complete" and len(manifest["files"]) == 2
    finally:
        await network.close()


async def test_ugoira_generated_names_do_not_overwrite_source_frames(tmp_path):
    if not find_ffmpeg(Config()):
        pytest.skip("ffmpeg not installed")
    zip_path = tmp_path / "names.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("frame_000000.png", png((255, 0, 0)))
    out = await Downloader(Config(), FakeNetwork())._compose_ugoira(
        zip_path, {"frames": [{"file": "frame_000000.png", "delay": 100}]}
    )
    assert out.is_file() and out.stat().st_size > 0
