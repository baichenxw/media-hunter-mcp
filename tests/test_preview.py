"""真实解码、字节预算、预览源和凭证隔离测试。"""

import asyncio
import random
import threading
from io import BytesIO

import httpx
import pytest
import respx
from PIL import Image

from media_mcp.config import Config, NetworkConfig
from media_mcp.models import AuthError, Post, SiteNetworkError, ValidationError
from media_mcp.network import Network
from media_mcp.preview import MAX_PREVIEW_BYTES, preview_post

E621_PREVIEW = "https://static1.e621.net/data/preview/aa/bb/test.jpg"


def image_bytes(size=(960, 480), mode="RGB", color="red", format="PNG", **kwargs):
    output = BytesIO()
    Image.new(mode, size, color).save(output, format=format, **kwargs)
    return output.getvalue()


def post(site="e621", url=E621_PREVIEW):
    return Post(site=site, id="123", url="https://e621.net/posts/123", preview_url=url)


@pytest.fixture
async def network():
    network = Network(
        Config(
            network=NetworkConfig(retries=1),
            sites={
                name: {"request_interval": 0} for name in ("e621", "rule34", "ehentai", "pixiv")
            },
        )
    )
    yield network
    await network.close()


@respx.mock
async def test_preview_is_small_jpeg_without_metadata_or_disk_writes(
    network, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    exif = Image.Exif()
    exif[0x010E] = "private source comment"
    exif[0x0112] = 6
    respx.get(E621_PREVIEW).respond(content=image_bytes(exif=exif))
    result = await preview_post(network, post())
    assert result.mime_type == "image/jpeg"
    assert len(result.data) <= MAX_PREVIEW_BYTES
    assert (result.width, result.height) == (320, 640)
    assert result.metadata()["size_bytes"] == len(result.data)
    assert "source_url" not in result.metadata()
    assert list(tmp_path.iterdir()) == []
    with Image.open(BytesIO(result.data)) as decoded:
        assert decoded.format == "JPEG"
        assert not decoded.getexif()
        assert not decoded.info.get("icc_profile")
        assert not decoded.info.get("comment")
        assert decoded.mode == "RGB"


@respx.mock
async def test_alpha_uses_white_background_and_does_not_enlarge(network):
    respx.get(E621_PREVIEW).respond(
        content=image_bytes(size=(20, 10), mode="RGBA", color=(0, 0, 0, 0))
    )
    result = await preview_post(network, post())
    assert (result.width, result.height) == (20, 10)
    with Image.open(BytesIO(result.data)) as decoded:
        assert decoded.getpixel((0, 0)) == (255, 255, 255)


@respx.mock
async def test_gif_first_frame_and_gallery_cover_are_explicit(network):
    output = BytesIO()
    Image.new("RGB", (32, 16), "red").save(
        output,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (32, 16), "blue")],
        duration=100,
    )
    url = "https://ehgt.org/t/cover.gif"
    respx.get(url).respond(content=output.getvalue(), headers={"content-type": "image/gif"})
    result = await preview_post(network, post("ehentai", url))
    assert result.animated_first_frame
    assert result.source_kind == "gallery_cover"
    with Image.open(BytesIO(result.data)) as decoded:
        red, green, blue = decoded.getpixel((16, 8))
        assert red > 245 and green < 10 and blue < 10


@respx.mock
async def test_high_entropy_image_obeys_output_budget(network):
    output = BytesIO()
    Image.frombytes("RGB", (640, 640), random.Random(0).randbytes(640 * 640 * 3)).save(
        output, format="PNG"
    )
    respx.get(E621_PREVIEW).respond(content=output.getvalue())
    result = await preview_post(network, post())
    assert len(result.data) <= MAX_PREVIEW_BYTES
    with Image.open(BytesIO(result.data)) as decoded:
        assert decoded.size == (result.width, result.height)
        decoded.load()


@pytest.mark.parametrize(
    "url",
    [
        None,
        "http://static1.e621.net/test.jpg",
        "https://static1.e621.net.evil.example/test.jpg",
        "https://127.0.0.1/test.jpg",
        "https://[::1]/test.jpg",
        "https://localhost/test.jpg",
        "https://user:password@static1.e621.net/test.jpg",
        "https://static1.e621.net:444/test.jpg",
        "https://static1.e621.net/test.jpg#fragment",
        "https://static1.e621.net/\n.jpg",
        "https://i.pximg.net/other-site.jpg",
    ],
)
@respx.mock
async def test_rejects_untrusted_source_before_network(network, url):
    with pytest.raises(ValidationError):
        await preview_post(network, post(url=url))
    assert respx.calls.call_count == 0


@respx.mock
async def test_refuses_redirect_before_contacting_untrusted_host(network):
    respx.get(E621_PREVIEW).respond(302, headers={"location": "https://127.0.0.1/secret"})
    with pytest.raises(ValidationError):
        await preview_post(network, post())
    assert respx.calls.call_count == 1


@respx.mock
async def test_allows_valid_relative_redirect_with_fresh_validation(network):
    respx.get(E621_PREVIEW).respond(302, headers={"location": "/data/preview/next.jpg"})
    respx.get("https://static1.e621.net/data/preview/next.jpg").respond(content=image_bytes())
    result = await preview_post(network, post())
    assert result.source_url.endswith("/data/preview/next.jpg")
    assert respx.calls.call_count == 2


@respx.mock
async def test_limits_redirects(network):
    respx.get(E621_PREVIEW).respond(302, headers={"location": E621_PREVIEW})
    with pytest.raises(SiteNetworkError, match="重定向"):
        await preview_post(network, post())
    assert respx.calls.call_count == 4


@respx.mock
async def test_pixiv_uses_configured_mirror_prefix_and_no_cookie_or_auth(network):
    original = "https://i.pximg.net/img-master/safe.jpg"
    mirrored = "https://mirror.example/proxy/img-master/safe.jpg"
    client = network._client(False)
    client.cookies.set("test_secret", "do-not-send", domain="mirror.example")
    route = respx.get(mirrored).respond(content=image_bytes())
    result = await preview_post(
        network, post("pixiv", original), image_mirror="https://mirror.example/proxy"
    )
    assert result.source_url == mirrored
    request = route.calls.last.request
    assert request.headers["referer"] == "https://www.pixiv.net/"
    assert request.headers["accept-encoding"] == "identity"
    assert "cookie" not in request.headers
    assert "authorization" not in request.headers
    assert respx.calls.call_count == 1


@pytest.mark.parametrize(
    "mirror",
    [
        "https://localhost",
        "https://192.168.1.1",
        "https://[::1]",
        "http://mirror.example",
        "https://mirror.example:8443",
        "https://host.internal",
    ],
)
@respx.mock
async def test_rejects_unsafe_configured_mirror(network, mirror):
    with pytest.raises(ValidationError):
        await preview_post(network, post("pixiv", "https://i.pximg.net/a.jpg"), image_mirror=mirror)
    assert respx.calls.call_count == 0


@respx.mock
async def test_mirror_does_not_authorize_arbitrary_metadata_host(network):
    with pytest.raises(ValidationError):
        await preview_post(
            network,
            post("pixiv", "https://mirror.example/a.jpg"),
            image_mirror="https://mirror.example",
        )
    assert respx.calls.call_count == 0


@respx.mock
async def test_mirror_redirect_cannot_escape_configured_prefix(network):
    respx.get("https://mirror.example/proxy/a.jpg").respond(
        302, headers={"location": "/private/a.jpg"}
    )
    with pytest.raises(ValidationError):
        await preview_post(
            network,
            post("pixiv", "https://i.pximg.net/a.jpg"),
            image_mirror="https://mirror.example/proxy",
        )
    assert respx.calls.call_count == 1


@respx.mock
async def test_exhentai_thumbnail_cookies_are_not_forwarded(network):
    url = "https://s.exhentai.org/t/cover.jpg"
    network._client(False).cookies.set("ipb_pass_hash", "do-not-send", domain=".exhentai.org")
    route = respx.get(url).respond(403)
    with pytest.raises(AuthError):
        await preview_post(network, post("ehentai", url))
    assert "cookie" not in route.calls.last.request.headers
    assert respx.calls.call_count == 1


@pytest.mark.parametrize(
    ("content", "headers"),
    [
        (b"<html>login page</html>", {"content-type": "text/html"}),
        (b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', {"content-type": "image/svg+xml"}),
        (b"not an image", {"content-type": "image/jpeg"}),
        (b"too big", {"content-length": str(8 * 1024 * 1024 + 1)}),
    ],
)
@respx.mock
async def test_rejects_non_images_compressed_http_and_large_declared_body(
    network, content, headers
):
    respx.get(E621_PREVIEW).mock(
        return_value=httpx.Response(200, stream=httpx.ByteStream(content), headers=headers)
    )
    with pytest.raises(ValidationError):
        await preview_post(network, post())


class CountingStream(httpx.AsyncByteStream):
    def __init__(self):
        self.chunks = 0
        self.closed = False

    async def __aiter__(self):
        for _ in range(1024):
            self.chunks += 1
            yield b"x" * (64 * 1024)

    async def aclose(self):
        self.closed = True


@respx.mock
async def test_http_compression_is_rejected_before_reading_body(network):
    stream = CountingStream()
    respx.get(E621_PREVIEW).mock(
        return_value=httpx.Response(200, stream=stream, headers={"content-encoding": "gzip"})
    )
    with pytest.raises(ValidationError, match="identity"):
        await preview_post(network, post())
    assert stream.chunks == 0
    assert stream.closed


@respx.mock
async def test_streaming_limit_stops_before_reading_entire_body_and_closes(network):
    stream = CountingStream()
    respx.get(E621_PREVIEW).mock(return_value=httpx.Response(200, stream=stream))
    with pytest.raises(ValidationError, match="8 MiB"):
        await preview_post(network, post())
    assert stream.chunks == 129
    assert stream.closed


@respx.mock
async def test_pixel_limit_checked_before_decoding(network, monkeypatch):
    import media_mcp.preview as preview

    monkeypatch.setattr(preview, "MAX_SOURCE_PIXELS", 10_000)
    respx.get(E621_PREVIEW).respond(content=image_bytes(size=(101, 100)))
    with pytest.raises(ValidationError, match="像素"):
        await preview_post(network, post())


@respx.mock
async def test_truncated_image_is_reported_as_validation_error(network):
    respx.get(E621_PREVIEW).respond(content=image_bytes(format="JPEG")[:200])
    with pytest.raises(ValidationError, match="图片"):
        await preview_post(network, post())


async def test_cancelled_compression_keeps_running_worker_limit_and_discards_queue(monkeypatch):
    import media_mcp.preview as preview

    loop = asyncio.get_running_loop()
    started = asyncio.Queue()
    release = threading.Event()
    guard = threading.Lock()
    state = {"active": 0, "peak": 0, "ids": []}

    async def read_source(*args):
        return b"test", E621_PREVIEW

    def compress(data, source_post, source_url):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["ids"].append(source_post.id)
        loop.call_soon_threadsafe(started.put_nowait, source_post.id)
        try:
            if not release.wait(5):
                raise RuntimeError("测试未及时释放压缩线程")
            return preview.PreviewImage(
                data=b"result",
                mime_type="image/jpeg",
                width=1,
                height=1,
                source_url=source_url,
                source_kind="artwork_thumbnail",
                animated_first_frame=False,
            )
        finally:
            with guard:
                state["active"] -= 1

    monkeypatch.setattr(preview, "_read_preview", read_source)
    monkeypatch.setattr(preview, "_compress_preview", compress)
    tasks = []

    def schedule(post_id):
        source_post = post()
        source_post.id = post_id
        task = asyncio.create_task(preview_post(None, source_post))
        tasks.append(task)
        return task

    try:
        first, second = schedule("1"), schedule("2")
        assert {await asyncio.wait_for(started.get(), 2) for _ in range(2)} == {"1", "2"}
        queued = schedule("3")
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued

        # 两个请求均取消，但其正在执行的线程仍须占用真正的 CPU 并发槽位。
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        replacement = schedule("4")
        await asyncio.sleep(0.05)
        with guard:
            assert state["active"] == state["peak"] == 2
            assert set(state["ids"]) == {"1", "2"}

        release.set()
        result = await asyncio.wait_for(replacement, 2)
        assert result.data == b"result"
        with guard:
            assert state["peak"] == 2
            assert set(state["ids"]) == {"1", "2", "4"}
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        async with asyncio.timeout(2):
            while state["active"]:
                await asyncio.sleep(0.005)
