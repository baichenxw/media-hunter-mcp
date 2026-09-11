"""配置加载测试。"""

from pathlib import Path

import pytest

from media_mcp.config import Config, find_config_path, load_config


def test_load_config_full(tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'download_root = "D:/media"\n'
        "[network]\n"
        'proxy = "http://127.0.0.1:7897"\n'
        "timeout = 10\n"
        "retries = 2\n"
        "[sites.e621]\n"
        'username = "u"\n'
        'api_key = "k"\n',
        encoding="utf-8",
    )
    config = load_config(cfg)
    assert config.download_root == Path("D:/media")
    assert config.network.proxy == "http://127.0.0.1:7897"
    assert config.network.timeout == 10.0
    assert config.network.retries == 2
    assert config.site_get("e621", "username") == "u"
    assert config.site_get("e621", "missing", "fallback") == "fallback"
    assert config.site_get("unknown", "any", "fallback") == "fallback"


def test_load_empty_toml_uses_defaults(tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("", encoding="utf-8")
    config = load_config(cfg)
    assert config.network.proxy == ""
    assert config.network.timeout == 30.0
    assert config.network.retries == 3


def test_load_config_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_find_config_path_env(monkeypatch, tmp_path: Path):
    cfg = tmp_path / "c.toml"
    cfg.write_text("", encoding="utf-8")
    monkeypatch.setenv("MEDIA_HUNTER_CONFIG", str(cfg))
    assert find_config_path() == cfg


def test_default_config():
    config = Config()
    assert config.network.proxy == ""
    assert config.site_get("e621", "username", "") == ""


@pytest.mark.parametrize(
    "network", ["timeout = -1", "timeout = nan", "retries = 0", "retries = 11"]
)
def test_invalid_network_rejected(tmp_path, network):
    from media_mcp.models import ValidationError

    path = tmp_path / "config.toml"
    path.write_text("[network]\n" + network, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(path)


def test_relative_download_root_uses_config_location(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('download_root = "media"', encoding="utf-8-sig")
    monkeypatch.chdir(tmp_path.parent)
    assert load_config(path).download_root == tmp_path / "media"


@pytest.mark.parametrize(
    "settings",
    [
        {"download_concurrency": 0},
        {"download_delay": -1},
        {"mirror_base": "file:///secret"},
        {"ugoira_format": "exe"},
    ],
)
def test_invalid_site_options(settings):
    from media_mcp.models import ValidationError

    with pytest.raises(ValidationError):
        Config(sites={"pixiv": settings})


@pytest.mark.parametrize(
    "contents, field",
    [
        ("[network]\nretries = 2.5", "network.retries"),
        ("[network]\nretries = true", "network.retries"),
        ('[network]\ntimeout = "SECRET"', "network.timeout"),
        ("[sites.pixiv]\ndownload_concurrency = 1.5", "sites.pixiv.download_concurrency"),
        ('[sites.ehentai]\nuse_exhentai = "false"', "sites.ehentai.use_exhentai"),
        ("network = []", "network"),
    ],
)
def test_config_errors_identify_field_without_echoing_value(tmp_path, contents, field):
    from media_mcp.models import ValidationError

    path = tmp_path / "bad.toml"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValidationError) as error:
        load_config(path)
    assert field in str(error.value) and "SECRET" not in str(error.value)
