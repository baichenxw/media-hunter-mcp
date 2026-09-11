"""配置加载：TOML 文件 + MEDIA_HUNTER_CONFIG 环境变量。"""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .models import ValidationError

DEFAULT_DOWNLOAD_ROOT = Path.home() / "Downloads" / "media-hunter"


def validate_number(value, name, *, minimum, maximum=None, integer=False, exclusive=False):
    expected = (int,) if integer else (int, float)
    if (
        isinstance(value, bool)
        or not isinstance(value, expected)
        or not math.isfinite(value)
        or (value <= minimum if exclusive else value < minimum)
        or (maximum is not None and value > maximum)
    ):
        kind = "整数" if integer else "有限数值"
        bound = f"大于 {minimum}" if exclusive else f"不小于 {minimum}"
        if maximum is not None:
            bound += f" 且不大于 {maximum}"
        raise ValidationError(f"{name} 必须是{bound}的{kind}")


@dataclass
class NetworkConfig:
    proxy: str = ""
    timeout: float = 30.0
    retries: int = 3

    def __post_init__(self):
        validate_number(self.timeout, "network.timeout", minimum=0, exclusive=True)
        validate_number(self.retries, "network.retries", minimum=1, maximum=10, integer=True)
        if not isinstance(self.proxy, str):
            raise ValidationError("network.proxy 必须是代理 URL 字符串")
        try:
            proxy = urlsplit(self.proxy)
            valid_proxy = (
                proxy.scheme in {"http", "https", "socks5", "socks5h"}
                and proxy.hostname
                and (proxy.port is None or proxy.port > 0)
            )
        except ValueError:
            valid_proxy = False
        if self.proxy and not valid_proxy:
            raise ValidationError("network.proxy 必须是 HTTP(S) 或 SOCKS5 代理 URL")


@dataclass
class Config:
    download_root: Path = DEFAULT_DOWNLOAD_ROOT
    network: NetworkConfig = field(default_factory=NetworkConfig)
    sites: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.sites, dict):
            raise ValidationError("sites 必须是 TOML 表")
        for name, values in self.sites.items():
            if not isinstance(values, dict):
                raise ValidationError(f"sites.{name} 必须是 TOML 表")
            for key in ("request_interval", "download_delay"):
                if key in values:
                    validate_number(values[key], f"sites.{name}.{key}", minimum=0)
            validate_number(
                values.get("download_concurrency", 4),
                f"sites.{name}.download_concurrency",
                minimum=1,
                maximum=16,
                integer=True,
            )
            for key in ("use_exhentai", "allow_original"):
                if key in values and not isinstance(values[key], bool):
                    raise ValidationError(f"sites.{name}.{key} 必须是 true 或 false")
            for key in (
                "mirror_base",
                "exhentai_mirror_base",
                "api_base",
                "image_mirror",
                "oauth_base",
            ):
                if value := values.get(key):
                    try:
                        parts = urlsplit(value) if isinstance(value, str) else None
                        valid_port = parts is not None and (parts.port is None or parts.port > 0)
                    except ValueError:
                        parts, valid_port = None, False
                    if (
                        parts is None
                        or not valid_port
                        or parts.scheme not in {"http", "https"}
                        or not parts.hostname
                        or parts.username
                        or parts.query
                        or parts.fragment
                    ):
                        raise ValidationError(
                            f"sites.{name}.{key} 必须是无凭证、查询参数的 HTTP(S) 基地址"
                        )
        if self.site_get("pixiv", "ugoira_format", "gif") not in {"gif", "mp4", "zip"}:
            raise ValidationError("pixiv.ugoira_format 仅支持 gif、mp4、zip")

    def site_get(self, site: str, key: str, default=None):
        """读取 [sites.<site>] 表中的键；站点或键不存在时返回 default。"""
        value = self.sites.get(site, {}).get(key, default)
        return default if value is None else value


def find_config_path() -> Path | None:
    """按优先级查找配置文件：环境变量 > 当前目录 > 用户配置目录。"""
    env = os.environ.get("MEDIA_HUNTER_CONFIG")
    if env:
        return Path(env).expanduser()
    for candidate in (
        Path("config.toml"),
        Path(__file__).resolve().parents[2] / "config.toml",
        Path.home() / ".config" / "media-hunter" / "config.toml",
    ):
        if candidate.is_file():
            return candidate
    return None


def load_config(path: str | Path | None = None) -> Config:
    """加载配置；path 为 None 时自动查找，找不到任何文件时返回全默认配置。"""
    config_path = Path(path).expanduser() if path else find_config_path()
    if config_path is None:
        return Config()
    if not config_path.is_file():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    raw = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    network_raw = raw.get("network", {})
    if not isinstance(network_raw, dict):
        raise ValidationError("network 必须是 TOML 表")
    if "download_root" in raw and not isinstance(raw["download_root"], str):
        raise ValidationError("download_root 必须是路径字符串")
    root = Path(raw.get("download_root", str(DEFAULT_DOWNLOAD_ROOT))).expanduser()
    if not root.is_absolute():
        root = (config_path.resolve().parent / root).resolve()
    return Config(
        download_root=root,
        network=NetworkConfig(
            proxy=network_raw.get("proxy", ""),
            timeout=network_raw.get("timeout", 30.0),
            retries=network_raw.get("retries", 3),
        ),
        sites=raw.get("sites", {}),
    )
