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


@dataclass
class NetworkConfig:
    proxy: str = ""
    timeout: float = 30.0
    retries: int = 3

    def __post_init__(self):
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValidationError("network.timeout 必须大于 0")
        if not 1 <= self.retries <= 10:
            raise ValidationError("network.retries 必须为 1–10 次尝试")
        if self.proxy and urlsplit(self.proxy).scheme not in {"http", "https", "socks5", "socks5h"}:
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
                if key in values and (
                    not math.isfinite(float(values[key])) or float(values[key]) < 0
                ):
                    raise ValidationError(f"sites.{name}.{key} 必须是非负数")
            if not 1 <= int(values.get("download_concurrency", 4)) <= 16:
                raise ValidationError(f"sites.{name}.download_concurrency 必须为 1–16")
            for key in ("mirror_base", "api_base", "image_mirror", "oauth_base"):
                if value := values.get(key):
                    parts = urlsplit(value)
                    if (
                        parts.scheme not in {"http", "https"}
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
    config_path = Path(path) if path else find_config_path()
    if config_path is None:
        return Config()
    if not config_path.is_file():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    raw = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    network_raw = raw.get("network", {})
    root = Path(raw.get("download_root", str(DEFAULT_DOWNLOAD_ROOT))).expanduser()
    if not root.is_absolute():
        root = (config_path.resolve().parent / root).resolve()
    return Config(
        download_root=root,
        network=NetworkConfig(
            proxy=str(network_raw.get("proxy", "")),
            timeout=float(network_raw.get("timeout", 30.0)),
            retries=int(network_raw.get("retries", 3)),
        ),
        sites=dict(raw.get("sites", {})),
    )
