"""数据模型测试。"""

from media_mcp.models import DownloadTarget, MediaType, Post, QuotaError


def test_post_roundtrip():
    post = Post(
        site="e621",
        id="123",
        url="https://e621.net/posts/123",
        tags=["solo"],
        artist=["foo"],
        rating="s",
        score=42,
        media_type=MediaType.VIDEO,
        file_url="https://x/v.webm",
        extra={"md5": "abc"},
    )
    data = post.to_dict()
    assert data["media_type"] == "video"
    assert Post.from_dict(data) == post


def test_post_defaults():
    post = Post(site="rule34", id="1", url="u")
    assert post.media_type == MediaType.IMAGE
    assert post.page_count == 1
    assert post.score is None


def test_download_target_defaults():
    target = DownloadTarget(url="https://x/a.jpg", filename="a.jpg")
    assert target.page == 0
    assert target.headers == {}
    assert target.post_process is None


def test_error_envelope():
    err = QuotaError("配额用尽", hint="等待恢复")
    assert err.to_dict() == {"type": "quota", "message": "配额用尽", "hint": "等待恢复"}
    assert str(err) == "配额用尽"
