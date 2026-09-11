import json

import pytest

from media_mcp.cli import main, parser


@pytest.mark.parametrize(
    "command",
    [
        ["download", "pixiv", "1"],
        ["download-url", "https://www.pixiv.net/artworks/1"],
        ["download-search", "pixiv", "landscape"],
    ],
)
def test_cli_overwrite_option(command):
    assert not parser().parse_args(command).overwrite
    assert parser().parse_args([*command, "--overwrite"]).overwrite


def test_cli_config_error_is_actionable_and_json(tmp_path, monkeypatch, capsys):
    config = tmp_path / "bad.toml"
    config.write_text('[network]\ntimeout = "SECRET"', encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["media-hunter", "--config", str(config), "check"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    output = capsys.readouterr().out
    assert "network.timeout" in json.loads(output)["error"]["message"]
    assert "SECRET" not in output


@pytest.mark.parametrize(
    "operation", ["search", "get", "download", "download-url", "download-search", "check"]
)
def test_cli_ehentai_site_options(operation):
    tail = {
        "search": ["ehentai", "landscape"],
        "get": ["ehentai", "12/abc"],
        "download": ["ehentai", "12/abc"],
        "download-url": ["https://exhentai.org/g/12/abc/"],
        "download-search": ["ehentai", "landscape"],
        "check": [],
    }[operation]
    assert parser().parse_args([operation, *tail]).use_exhentai is None
    assert parser().parse_args([operation, *tail, "--use-exhentai"]).use_exhentai is True
    assert parser().parse_args([operation, *tail, "--no-use-exhentai"]).use_exhentai is False
    if operation.startswith("download"):
        assert parser().parse_args([operation, *tail, "--original"]).original
