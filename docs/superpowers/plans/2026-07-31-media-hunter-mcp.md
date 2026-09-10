# media-hunter-mcp 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建 Python + FastMCP 的 MCP 服务器，让 AI 通过 6 个工具在 e621 / rule34 / E-Hentai / pixiv 上搜索并下载图片、画廊、视频。

**Architecture:** 适配器模式。`sites/` 下每个站点一个适配器（统一接口 search / get_post / get_download_targets / parse_url / check），共享 `network.py`（代理 + 镜像 fallback + 限速）和 `downloader.py`（文件组织 + sidecar + ugoira 合成）。

**Tech Stack:** Python ≥3.11、uv、FastMCP 2.x、httpx、pixivpy3、beautifulsoup4、pytest + respx。

**项目根目录:** `C:/Projects/media-hunter-mcp`（所有相对路径相对此目录；bash 命令用 workdir 参数指向此目录）

**设计文档:** `docs/superpowers/specs/2026-07-31-media-hunter-mcp-design.md`

## Global Constraints

- 所有网络访问必须经过 `network.py` 的 `Network.request`；所有文件落盘必须经过 `downloader.py`。适配器不直接创建 httpx client、不写文件。
- 除显式标注 smoke 的测试外，任何测试不得访问真实网络（一律 respx / mock）。
- 限速默认值：e621 0.5s、rule34 1s、ehentai 2.5s、pixiv 1s（可通过 config `[sites.X] request_interval` 覆盖）。
- 工具返回统一信封：`{"success": True, "data": ...}` 或 `{"success": False, "error": {"type", "message", "hint"}}`。
- 代码注释、README、提交信息用中文；标识符用英文。
- 工作区当前不是 git 仓库：若用户批准则先 `git init`，各任务末尾的 commit 步骤照做；否则跳过 commit 步骤。
- 依赖固定写入 `pyproject.toml`，用 `uv sync` 安装，禁止往系统 Python 装包。

---

### Task 1: 项目脚手架 + 配置加载

**Files:**
- Create: `pyproject.toml`
- Create: `config.example.toml`
- Create: `src/media_mcp/__init__.py`
- Create: `src/media_mcp/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config(download_root: Path, network: NetworkConfig, sites: dict)`、`NetworkConfig(proxy: str, timeout: float, retries: int)`、`Config.site_get(site: str, key: str, default=None)`、`load_config(path=None) -> Config`、`find_config_path() -> Path | None`。后续所有任务依赖。

- [ ] **Step 1: 写 pyproject.toml 与包骨架**

`pyproject.toml`：

```toml
[project]
name = "media-hunter-mcp"
version = "0.1.0"
description = "搜索并下载 e621 / rule34 / E-Hentai / pixiv 资源的 MCP 服务器"
requires-python = ">=3.11"
dependencies = [
    "fastmcp>=2.0",
    "httpx>=0.27",
    "pixivpy3>=3.7.5",
    "beautifulsoup4>=4.12",
]

[project.scripts]
media-mcp = "media_mcp.server:main"

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/media_mcp"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

`src/media_mcp/__init__.py`：

```python
"""media-hunter-mcp：多站点媒体搜索下载 MCP 服务器。"""

__version__ = "0.1.0"
```

`config.example.toml`：

```toml
# media-hunter-mcp 配置示例。复制为 config.toml 后填写。
# 读取顺序：环境变量 MEDIA_HUNTER_CONFIG > 当前目录 config.toml > ~/.config/media-hunter/config.toml

# 下载根目录（必填，按站点自动分类）
download_root = "D:/Downloads/mcp-media"

[network]
# 代理地址（如梯子本地端口）；留空 "" 表示直连优先
proxy = "http://127.0.0.1:7897"
timeout = 30      # 秒
retries = 3       # 每个候选地址的重试次数

# ---- e621 ----
# 可选认证（提高限额）。user_agent 必须带描述性标识，不要伪装浏览器
[sites.e621]
username = ""
api_key = ""
# user_agent = "media-hunter-mcp/0.1 (by your-e621-username)"
# request_interval = 0.5
# mirror_base = "https://e621.net"   # 可替换为反代镜像

# ---- rule34.xxx ----
# 必填：注册账号后在 https://rule34.xxx/index.php?page=account&s=options 生成 API key
[sites.rule34]
user_id = ""
api_key = ""

# ---- E-Hentai / ExHentai ----
# cookie 从浏览器开发者工具复制：ipb_member_id=…; ipb_pass_hash=…; igneous=…
[sites.ehentai]
cookie = ""
use_exhentai = true
# mirror_base = "https://e-hentai.org"
# download_delay = 3    # 每张图片间隔秒数（保护配额）

# ---- pixiv ----
# refresh_token 用 gppt（pip install gppt）或浏览器 OAuth 流程获取
[sites.pixiv]
refresh_token = ""
# image_mirror = "https://i.pixiv.re"   # 图片 CDN 反代（备用）
# api_base = "https://app-api.pixiv.net" # API 反代（备用）
# ugoira_format = "gif"                 # gif 或 mp4
```

- [ ] **Step 2: 写失败测试**

`tests/test_config.py`：

```python
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
```

- [ ] **Step 3: 安装依赖并确认测试失败**

Run: `uv sync` 然后 `uv run pytest tests/test_config.py -v`
Expected: 5 个测试全部 FAIL（`ModuleNotFoundError: No module named 'media_mcp.config'`）

- [ ] **Step 4: 实现 config.py**

`src/media_mcp/config.py`：

```python
"""配置加载：TOML 文件 + MEDIA_HUNTER_CONFIG 环境变量。"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DOWNLOAD_ROOT = Path.home() / "Downloads" / "media-hunter"


@dataclass
class NetworkConfig:
    proxy: str = ""
    timeout: float = 30.0
    retries: int = 3


@dataclass
class Config:
    download_root: Path = DEFAULT_DOWNLOAD_ROOT
    network: NetworkConfig = field(default_factory=NetworkConfig)
    sites: dict = field(default_factory=dict)

    def site_get(self, site: str, key: str, default=None):
        """读取 [sites.<site>] 表中的键；站点或键不存在时返回 default。"""
        value = self.sites.get(site, {}).get(key, default)
        return default if value is None else value


def find_config_path() -> Path | None:
    """按优先级查找配置文件：环境变量 > 当前目录 > 用户配置目录。"""
    env = os.environ.get("MEDIA_HUNTER_CONFIG")
    if env:
        return Path(env)
    for candidate in (
        Path("config.toml"),
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
    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)
    network_raw = raw.get("network", {})
    return Config(
        download_root=Path(raw.get("download_root", str(DEFAULT_DOWNLOAD_ROOT))).expanduser(),
        network=NetworkConfig(
            proxy=str(network_raw.get("proxy", "")),
            timeout=float(network_raw.get("timeout", 30.0)),
            retries=int(network_raw.get("retries", 3)),
        ),
        sites=dict(raw.get("sites", {})),
    )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/test_config.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit（若已 git init，下同）**

```powershell
git init; git add pyproject.toml config.example.toml src tests
git commit -m "feat: 项目脚手架与配置加载"
```

---

### Task 2: 统一数据模型与错误类型

**Files:**
- Create: `src/media_mcp/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `MediaType(Enum)`；`Post`（含 `to_dict()` / `from_dict()`）；`DownloadTarget(url, filename, page, headers, post_process, post_process_meta)`；`DownloadedFile(path, page, size)`；`DownloadResult(post, directory, files, sidecar_path)`（含 `to_dict()`）；错误族 `MediaMcpError`（`error_type="internal"`，`.message`、`.hint`、`to_dict()`）及子类 `AuthError` / `QuotaError` / `NotFoundError` / `SiteNetworkError`（error_type 分别为 auth / quota / not_found / network）。

- [ ] **Step 1: 写失败测试**

`tests/test_models.py`：

```python
"""数据模型测试。"""
from media_mcp.models import DownloadTarget, MediaType, Post, QuotaError


def test_post_roundtrip():
    post = Post(
        site="e621", id="123", url="https://e621.net/posts/123",
        tags=["solo"], artist=["foo"], rating="s", score=42,
        media_type=MediaType.VIDEO, file_url="https://x/v.webm",
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
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'media_mcp.models'`）

- [ ] **Step 3: 实现 models.py**

`src/media_mcp/models.py`：

```python
"""统一数据模型与错误类型。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class MediaType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    GALLERY = "gallery"
    UGOIRA = "ugoira"


@dataclass
class Post:
    """四个站点归一化后的作品/画廊描述。"""

    site: str
    id: str
    url: str
    title: str = ""
    tags: list[str] = field(default_factory=list)
    artist: list[str] = field(default_factory=list)
    rating: str = ""
    score: float | None = None
    media_type: MediaType = MediaType.IMAGE
    file_url: str | None = None
    preview_url: str | None = None
    page_count: int = 1
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["media_type"] = self.media_type.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Post":
        data = dict(data)
        data["media_type"] = MediaType(data.get("media_type", "image"))
        return cls(**data)


@dataclass
class DownloadTarget:
    """一个待下载文件。post_process="ugoira" 时下载完成后调用合成流程。"""

    url: str
    filename: str
    page: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    post_process: str | None = None
    post_process_meta: dict = field(default_factory=dict)


@dataclass
class DownloadedFile:
    path: str
    page: int
    size: int


@dataclass
class DownloadResult:
    post: Post
    directory: str
    files: list[DownloadedFile]
    sidecar_path: str

    def to_dict(self) -> dict:
        return {
            "post": self.post.to_dict(),
            "directory": self.directory,
            "files": [asdict(f) for f in self.files],
            "sidecar_path": self.sidecar_path,
        }


class MediaMcpError(Exception):
    """工具层统一错误基类：error_type / message / hint。"""

    error_type = "internal"

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict:
        return {"type": self.error_type, "message": self.message, "hint": self.hint}


class AuthError(MediaMcpError):
    error_type = "auth"


class QuotaError(MediaMcpError):
    error_type = "quota"


class NotFoundError(MediaMcpError):
    error_type = "not_found"


class SiteNetworkError(MediaMcpError):
    error_type = "network"
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_models.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```powershell
git add src/media_mcp/models.py tests/test_models.py
git commit -m "feat: 统一数据模型与错误类型"
```

---

### Task 3: 网络层（代理 + 镜像 fallback + 限速）

**Files:**
- Create: `src/media_mcp/network.py`
- Test: `tests/test_network.py`

**Interfaces:**
- Consumes: `Config`、`Config.site_get`、`NetworkConfig`（Task 1）；`SiteNetworkError`（Task 2）。
- Produces: `RateLimiter(interval)`（`async acquire()`）；`Network(config)`：`async request(site, method, url, **kwargs) -> httpx.Response`（透传 headers/params/json/auth；仅网络错误与 429/5xx 重试并按 fallback 链切换，4xx 原样返回由适配器解释）、`async close()`。

- [ ] **Step 1: 写失败测试**

`tests/test_network.py`：

```python
"""网络层测试：限速、fallback 链、重试策略。"""
import time

import httpx
import pytest
import respx

from media_mcp.config import Config, NetworkConfig
from media_mcp.models import SiteNetworkError
from media_mcp.network import Network, RateLimiter


async def test_rate_limiter_spacing():
    limiter = RateLimiter(0.05)
    start = time.monotonic()
    await limiter.acquire()
    await limiter.acquire()
    assert time.monotonic() - start >= 0.04


async def test_fallback_to_mirror():
    config = Config(
        network=NetworkConfig(retries=1),
        sites={"e621": {"mirror_base": "https://mirror.example"}},
    )
    network = Network(config)
    with respx.mock:
        respx.get("https://e621.net/posts.json").mock(
            side_effect=httpx.ConnectError("boom")
        )
        respx.get("https://mirror.example/posts.json").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        resp = await network.request("e621", "GET", "https://e621.net/posts.json")
    assert resp.json() == {"ok": True}
    await network.close()


async def test_4xx_no_retry_returned_as_is():
    config = Config(network=NetworkConfig(retries=3))
    network = Network(config)
    with respx.mock:
        route = respx.get("https://e621.net/posts.json").mock(
            return_value=httpx.Response(403)
        )
        resp = await network.request("e621", "GET", "https://e621.net/posts.json")
    assert resp.status_code == 403
    assert route.call_count == 1
    await network.close()


async def test_all_candidates_fail_raises():
    config = Config(network=NetworkConfig(retries=1))
    network = Network(config)
    with respx.mock:
        respx.get("https://e621.net/posts.json").mock(
            side_effect=httpx.ConnectError("boom")
        )
        with pytest.raises(SiteNetworkError):
            await network.request("e621", "GET", "https://e621.net/posts.json")
    await network.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_network.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'media_mcp.network'`）

- [ ] **Step 3: 实现 network.py**

`src/media_mcp/network.py`：

```python
"""统一网络层：代理 / 镜像 fallback 链、每站点限速、重试。"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import Config
from .models import SiteNetworkError

DEFAULT_INTERVALS = {"e621": 0.5, "rule34": 1.0, "ehentai": 2.5, "pixiv": 1.0}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    """简单限速器：保证同一站点的请求间隔不小于 interval 秒。"""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._last + self.interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


def _swap_base(url: str, new_base: str) -> str:
    """把 url 的 scheme+host 替换为 new_base 的（用于镜像）。"""
    parts = urlsplit(url)
    base = urlsplit(new_base)
    return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, parts.fragment))


class Network:
    """所有站点共享的网络入口。

    fallback 链：配了代理 → 代理访问原站 → 代理访问镜像；
    未配代理 → 直连原站 → 直连镜像。仅网络错误与 429/5xx 触发
    重试与切换；4xx 原样返回，由适配器解释为认证/不存在等错误。
    """

    def __init__(self, config: Config):
        self._config = config
        self._limiters: dict[str, RateLimiter] = {}
        self._clients: dict[str, httpx.AsyncClient] = {}

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    def _client(self, use_proxy: bool) -> httpx.AsyncClient:
        proxy = self._config.network.proxy if use_proxy else ""
        key = proxy or "direct"
        if key not in self._clients:
            self._clients[key] = httpx.AsyncClient(
                proxy=proxy or None,
                timeout=self._config.network.timeout,
                follow_redirects=True,
            )
        return self._clients[key]

    def _limiter(self, site: str) -> RateLimiter:
        if site not in self._limiters:
            interval = float(
                self._config.site_get(site, "request_interval", DEFAULT_INTERVALS.get(site, 1.0))
            )
            self._limiters[site] = RateLimiter(interval)
        return self._limiters[site]

    def _attempts(self, site: str, url: str) -> list[tuple[httpx.AsyncClient, str]]:
        proxy = self._config.network.proxy
        attempts: list[tuple[httpx.AsyncClient, str]] = []
        attempts.append((self._client(bool(proxy)), url))
        mirror = self._config.site_get(site, "mirror_base")
        if mirror:
            attempts.append((self._client(bool(proxy)), _swap_base(url, str(mirror))))
        return attempts

    async def request(self, site: str, method: str, url: str, **kwargs) -> httpx.Response:
        await self._limiter(site).acquire()
        retries = max(1, self._config.network.retries)
        last_error: Exception | None = None
        for client, candidate in self._attempts(site, url):
            for attempt in range(retries):
                try:
                    response = await client.request(method, candidate, **kwargs)
                except httpx.TransportError as exc:
                    last_error = exc
                    await asyncio.sleep(min(2 ** attempt, 8))
                    continue
                if response.status_code in RETRYABLE_STATUS:
                    last_error = SiteNetworkError(f"HTTP {response.status_code}", hint=candidate)
                    await asyncio.sleep(min(2 ** attempt, 8))
                    continue
                return response
        raise SiteNetworkError(
            f"请求失败（已尝试全部 fallback）: {url}",
            hint=str(last_error) if last_error else "",
        )
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_network.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```powershell
git add src/media_mcp/network.py tests/test_network.py
git commit -m "feat: 网络层（代理/镜像 fallback、限速、重试）"
```

---

### Task 4: 下载引擎（文件组织 + sidecar + ugoira 合成）

**Files:**
- Create: `src/media_mcp/downloader.py`
- Test: `tests/test_downloader.py`

**Interfaces:**
- Consumes: `Config`（Task 1）、`Post / DownloadTarget / DownloadedFile / DownloadResult / NotFoundError / SiteNetworkError`（Task 2）、`Network`（Task 3）。
- Produces: `sanitize(name, max_len=60) -> str`；`Downloader(config, network)`：`directory_for(post, subdir=None) -> Path`、`async download(post, targets, subdir=None) -> DownloadResult`、`_compose_ugoira(zip_path, meta) -> Path`（同步函数，内部调 ffmpeg）。

- [ ] **Step 1: 写失败测试**

`tests/test_downloader.py`：

```python
"""下载引擎测试：目录组织、sidecar、ugoira 合成。"""
import json
import subprocess
import zipfile
from pathlib import Path

import httpx

from media_mcp.config import Config
from media_mcp.downloader import Downloader, sanitize
from media_mcp.models import DownloadTarget, Post


class FakeNetwork:
    """测试用网络桩：所有请求返回固定字节。"""

    def __init__(self, content: bytes = b"img"):
        self.content = content
        self.urls: list[str] = []

    async def request(self, site, method, url, **kwargs):
        self.urls.append(url)
        return httpx.Response(200, content=self.content)


def test_sanitize():
    assert sanitize("a/b:c*d") == "a_b_c_d"
    assert sanitize("") == "untitled"
    assert sanitize("  name.  ") == "name"
    assert len(sanitize("x" * 100)) == 60


async def test_download_pixiv_organization(tmp_path: Path):
    config = Config(download_root=tmp_path)
    downloader = Downloader(config, FakeNetwork())
    post = Post(site="pixiv", id="100", url="u", title="T", artist=["作者A"], extra={"user_id": 7})
    targets = [
        DownloadTarget(url="https://i.pximg.net/p0.jpg", filename="100_p0.jpg", page=0),
        DownloadTarget(url="https://i.pximg.net/p1.jpg", filename="100_p1.jpg", page=1),
    ]
    result = await downloader.download(post, targets)
    assert Path(result.directory) == tmp_path / "pixiv" / "作者A_7"
    assert (Path(result.directory) / "100_p0.jpg").exists()
    assert result.files[1].page == 1
    sidecar = json.loads(Path(result.sidecar_path).read_text(encoding="utf-8"))
    assert sidecar["post"]["id"] == "100"
    assert len(sidecar["files"]) == 2


async def test_download_ehentai_gallery_sidecar(tmp_path: Path):
    config = Config(download_root=tmp_path)
    downloader = Downloader(config, FakeNetwork())
    post = Post(
        site="ehentai", id="12345/abc", url="u", title="画廊/标题",
        extra={"gid": "12345", "token": "abc"},
    )
    targets = [DownloadTarget(url="https://x/001.jpg", filename="001.jpg", page=1)]
    result = await downloader.download(post, targets)
    assert "12345_画廊_标题" in result.directory
    assert Path(result.sidecar_path).name == "gallery.json"


def test_compose_ugoira(tmp_path: Path, monkeypatch):
    zip_path = tmp_path / "778_ugoira.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("000.jpg", b"a")
        zf.writestr("001.jpg", b"b")
    config = Config(download_root=tmp_path)
    downloader = Downloader(config, FakeNetwork())

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"gif-bytes")  # 模拟 ffmpeg 产出
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = downloader._compose_ugoira(zip_path, {"delays": [100, 120]})
    assert out.suffix == ".gif" and out.exists()
    assert not zip_path.exists()          # zip 已清理
    assert not (tmp_path / "778_ugoira_frames").exists()  # 临时目录已清理
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_downloader.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'media_mcp.downloader'`）

- [ ] **Step 3: 实现 downloader.py**

`src/media_mcp/downloader.py`：

```python
"""共享下载引擎：目录组织、sidecar 元数据、ugoira 合成。"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .models import (
    DownloadedFile,
    DownloadResult,
    DownloadTarget,
    NotFoundError,
    Post,
    SiteNetworkError,
)
from .network import Network

WINDOWS_RESERVED = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def sanitize(name: str, max_len: int = 60) -> str:
    """清理 Windows 文件系统不允许的字符，截断到 max_len。"""
    cleaned = WINDOWS_RESERVED.sub("_", name).strip(" .")
    return cleaned[:max_len].strip(" .") or "untitled"


class Downloader:
    """统一落盘：适配器给 DownloadTarget，本类决定目录与命名规则之外的组织。"""

    def __init__(self, config: Config, network: Network):
        self._config = config
        self._network = network

    def directory_for(self, post: Post, subdir: str | None = None) -> Path:
        root = Path(self._config.download_root)
        if subdir:
            return root / post.site / sanitize(subdir, 40)
        today = datetime.now().strftime("%Y-%m-%d")
        if post.site in ("e621", "rule34"):
            return root / post.site / today
        if post.site == "ehentai":
            gid = post.extra.get("gid") or post.id.split("/")[0]
            return root / "ehentai" / f"{gid}_{sanitize(post.title)}"
        if post.site == "pixiv":
            author = sanitize(post.artist[0]) if post.artist else "unknown"
            return root / "pixiv" / f"{author}_{post.extra.get('user_id', 0)}"
        return root / post.site

    async def download(
        self, post: Post, targets: list[DownloadTarget], subdir: str | None = None
    ) -> DownloadResult:
        directory = self.directory_for(post, subdir)
        directory.mkdir(parents=True, exist_ok=True)
        files: list[DownloadedFile] = []
        delay = float(self._config.site_get(post.site, "download_delay", 0) or 0)
        for target in targets:
            path = directory / sanitize(target.filename, 120)
            await self._download_one(post.site, target, path)
            if target.post_process == "ugoira":
                path = await asyncio.to_thread(self._compose_ugoira, path, target.post_process_meta)
            files.append(DownloadedFile(path=str(path), page=target.page, size=path.stat().st_size))
            if delay:
                await asyncio.sleep(delay)
        sidecar = self._write_sidecar(post, directory, files)
        return DownloadResult(post=post, directory=str(directory), files=files, sidecar_path=str(sidecar))

    async def _download_one(self, site: str, target: DownloadTarget, path: Path) -> None:
        response = await self._network.request(site, "GET", target.url, headers=dict(target.headers))
        if response.status_code == 404:
            raise NotFoundError(f"文件不存在: {target.url}")
        if response.status_code != 200:
            raise SiteNetworkError(f"下载失败 HTTP {response.status_code}: {target.url}")
        path.write_bytes(response.content)

    def _write_sidecar(self, post: Post, directory: Path, files: list[DownloadedFile]) -> Path:
        name = "gallery.json" if post.site == "ehentai" else f"{post.id}.json"
        path = directory / name
        payload = {
            "post": post.to_dict(),
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
            "files": [{"path": f.path, "page": f.page, "size": f.size} for f in files],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _compose_ugoira(self, zip_path: Path, meta: dict) -> Path:
        """解压 ugoira zip → 按帧延时合成（需本机 ffmpeg）。返回输出文件路径。"""
        delays: list[int] = meta.get("delays", [])
        work = zip_path.parent / f"{zip_path.stem}_frames"
        work.mkdir(exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(work)
        frames = sorted(p for p in work.glob("*.*") if p.suffix.lower() in IMAGE_EXTS)
        if not frames:
            raise SiteNetworkError(f"ugoira 解压后无帧图片: {zip_path}")
        fmt = str(self._config.site_get("pixiv", "ugoira_format", "gif"))
        out = zip_path.with_suffix(f".{fmt}")
        concat = work / "concat.txt"
        lines: list[str] = []
        for i, frame in enumerate(frames):
            delay = (delays[i] if i < len(delays) else 100) / 1000
            lines.append(f"file '{frame.name}'")
            lines.append(f"duration {delay:.3f}")
        lines.append(f"file '{frames[-1].name}'")
        concat.write_text("\n".join(lines), encoding="utf-8")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat)]
        if fmt == "gif":
            cmd += ["-vf", "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse", "-loop", "0"]
        else:
            cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        cmd.append(str(out))
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(work))
        if result.returncode != 0 or not out.exists():
            raise SiteNetworkError("ffmpeg 合成 ugoira 失败", hint=(result.stderr or "")[-400:])
        zip_path.unlink()
        shutil.rmtree(work)
        return out
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_downloader.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```powershell
git add src/media_mcp/downloader.py tests/test_downloader.py
git commit -m "feat: 下载引擎（目录组织、sidecar、ugoira ffmpeg 合成）"
```

---

### Task 5: 适配器基类 + e621 适配器

**Files:**
- Create: `src/media_mcp/sites/__init__.py`
- Create: `src/media_mcp/sites/base.py`
- Create: `src/media_mcp/sites/e621.py`
- Test: `tests/test_e621.py`

**Interfaces:**
- Consumes: `Config`（Task 1）、`Post / DownloadTarget / MediaType / AuthError / NotFoundError / SiteNetworkError`（Task 2）、`Network`（Task 3）。
- Produces: `SiteAdapter` 抽象基类：`name: str`、`async search(query, limit=20, page=1, min_score=None, rating=None) -> list[Post]`、`async get_post(post_id) -> Post`、`async get_download_targets(post) -> list[DownloadTarget]`、`parse_url(url) -> str | None`、`async check() -> dict`；`E621Adapter(name="e621")`。

- [ ] **Step 1: 写失败测试**

`tests/test_e621.py`：

```python
"""e621 适配器测试。"""
import httpx
import respx

from media_mcp.config import Config, NetworkConfig
from media_mcp.models import MediaType
from media_mcp.network import Network
from media_mcp.sites.e621 import E621Adapter

E621_POST = {
    "id": 123,
    "tags": {
        "artist": ["foo"], "general": ["solo"], "species": ["wolf"],
        "character": [], "copyright": [], "meta": [], "lore": [], "invalid": [],
    },
    "rating": "s",
    "score": {"up": 50, "down": -8, "total": 42},
    "file": {"url": "https://static1.e621.net/data/ab/cd/abcd.jpg", "ext": "jpg",
             "md5": "abcdef0123456789abcdef0123456789"},
    "preview": {"url": "https://static1.e621.net/data/preview/ab/cd/abcd.jpg"},
}


async def test_search_parses_posts_and_builds_tags():
    config = Config(network=NetworkConfig(retries=1))
    network = Network(config)
    adapter = E621Adapter(config, network)
    with respx.mock:
        route = respx.get("https://e621.net/posts.json").mock(
            return_value=httpx.Response(200, json={"posts": [E621_POST]})
        )
        posts = await adapter.search("solo", limit=5, min_score=10, rating="s")
        request = route.calls.last.request
        assert request.url.params["tags"] == "solo rating:s score:>=10"
        assert request.headers["user-agent"]
    post = posts[0]
    assert post.id == "123"
    assert post.artist == ["foo"]
    assert post.score == 42
    assert post.media_type == MediaType.IMAGE
    assert "wolf" in post.tags and "solo" in post.tags
    await network.close()


async def test_get_post_not_found():
    config = Config(network=NetworkConfig(retries=1))
    network = Network(config)
    adapter = E621Adapter(config, network)
    with respx.mock:
        respx.get("https://e621.net/posts.json").mock(
            return_value=httpx.Response(200, json={"posts": []})
        )
        from media_mcp.models import NotFoundError
        import pytest

        with pytest.raises(NotFoundError):
            await adapter.get_post("999999")
    await network.close()


async def test_targets_filename_and_parse_url():
    adapter = E621Adapter(Config(), None)
    post = E621Adapter._to_post(E621_POST)
    (target,) = await adapter.get_download_targets(post)
    assert target.filename == "123_foo_abcdef01.jpg"
    assert target.url == E621_POST["file"]["url"]
    assert adapter.parse_url("https://e621.net/posts/123") == "123"
    assert adapter.parse_url("https://www.google.com/") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_e621.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'media_mcp.sites'`）

- [ ] **Step 3: 实现 sites/base.py 与 sites/e621.py**

`src/media_mcp/sites/__init__.py`：

```python
"""站点适配器包。"""
```

`src/media_mcp/sites/base.py`：

```python
"""站点适配器抽象接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Config
from ..models import DownloadTarget, Post
from ..network import Network


class SiteAdapter(ABC):
    """每个站点一个适配器：负责站点语义与统一模型之间的转换。

    网络访问一律经 self._network.request，不写文件、不直接创建 httpx client。
    """

    name: str = ""

    def __init__(self, config: Config, network: Network):
        self._config = config
        self._network = network

    @abstractmethod
    async def search(
        self,
        query: str,
        limit: int = 20,
        page: int = 1,
        min_score: float | None = None,
        rating: str | None = None,
    ) -> list[Post]:
        """按站点原生语法搜索，返回统一 Post 列表。"""

    @abstractmethod
    async def get_post(self, post_id: str) -> Post:
        """获取单个作品/画廊完整元数据；不存在抛 NotFoundError。"""

    @abstractmethod
    async def get_download_targets(self, post: Post) -> list[DownloadTarget]:
        """展开为下载目标列表（单文件 1 项，画廊 N 项）。"""

    @abstractmethod
    def parse_url(self, url: str) -> str | None:
        """识别本站点作品 URL，返回 post_id；无法识别返回 None。"""

    @abstractmethod
    async def check(self) -> dict:
        """自检：{"ok": bool, "detail": str}，不抛异常。"""
```

`src/media_mcp/sites/e621.py`：

```python
"""e621 适配器：官方 JSON API（/posts.json）。"""
from __future__ import annotations

import re

from ..models import (
    AuthError,
    DownloadTarget,
    MediaType,
    NotFoundError,
    Post,
    SiteNetworkError,
)
from .base import SiteAdapter

VIDEO_EXTS = {"webm", "mp4"}
DEFAULT_UA = "media-hunter-mcp/0.1 (by local user)"


class E621Adapter(SiteAdapter):
    name = "e621"
    BASE = "https://e621.net"

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": str(self._config.site_get("e621", "user_agent", DEFAULT_UA))}

    def _auth(self) -> tuple[str, str] | None:
        username = str(self._config.site_get("e621", "username", "") or "")
        api_key = str(self._config.site_get("e621", "api_key", "") or "")
        return (username, api_key) if username and api_key else None

    async def _get_posts(self, params: dict) -> list[dict]:
        response = await self._network.request(
            self.name, "GET", f"{self.BASE}/posts.json",
            headers=self._headers(), params=params, auth=self._auth(),
        )
        if response.status_code in (401, 403):
            raise AuthError(
                "e621 拒绝了请求",
                hint="检查 [sites.e621] 的 username/api_key；User-Agent 不要伪装浏览器",
            )
        if response.status_code != 200:
            raise SiteNetworkError(f"e621 返回 HTTP {response.status_code}")
        return response.json().get("posts", [])

    async def search(self, query, limit=20, page=1, min_score=None, rating=None):
        tags = query.strip()
        if rating:
            tags += f" rating:{rating}"
        if min_score is not None:
            tags += f" score:>={min_score}"
        posts = await self._get_posts({"tags": tags.strip(), "limit": limit, "page": page})
        return [self._to_post(p) for p in posts]

    async def get_post(self, post_id: str) -> Post:
        posts = await self._get_posts({"tags": f"id:{post_id}", "limit": 1})
        if not posts:
            raise NotFoundError(f"e621 找不到作品 {post_id}")
        return self._to_post(posts[0])

    async def get_download_targets(self, post: Post) -> list[DownloadTarget]:
        if not post.file_url:
            raise NotFoundError(f"e621 作品 {post.id} 无可用文件（可能被删除或受限）")
        artist = post.artist[0] if post.artist else "unknown"
        md5 = str(post.extra.get("md5", ""))[:8]
        ext = str(post.extra.get("ext", "jpg"))
        return [DownloadTarget(url=post.file_url, filename=f"{post.id}_{artist}_{md5}.{ext}")]

    def parse_url(self, url: str) -> str | None:
        match = re.search(r"e621\.net/posts/(\d+)", url)
        return match.group(1) if match else None

    async def check(self) -> dict:
        try:
            await self._get_posts({"limit": 1})
        except Exception as exc:  # noqa: BLE001 自检不抛异常
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "detail": "posts.json 可访问"}

    @staticmethod
    def _to_post(raw: dict) -> Post:
        tags = raw.get("tags", {})
        flat = [tag for group in tags.values() for tag in group]
        file_info = raw.get("file", {})
        ext = file_info.get("ext", "")
        return Post(
            site="e621",
            id=str(raw["id"]),
            url=f"{E621Adapter.BASE}/posts/{raw['id']}",
            tags=flat,
            artist=list(tags.get("artist", [])),
            rating=raw.get("rating", ""),
            score=raw.get("score", {}).get("total"),
            media_type=MediaType.VIDEO if ext in VIDEO_EXTS else MediaType.IMAGE,
            file_url=file_info.get("url"),
            preview_url=raw.get("preview", {}).get("url"),
            extra={"md5": file_info.get("md5", ""), "ext": ext},
        )
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_e621.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```powershell
git add src/media_mcp/sites tests/test_e621.py
git commit -m "feat: 适配器基类与 e621 适配器"
```

---

### Task 6: rule34 适配器

**Files:**
- Create: `src/media_mcp/sites/rule34.py`
- Test: `tests/test_rule34.py`

**Interfaces:**
- Consumes: `SiteAdapter`（Task 5）、模型与错误（Task 2）、`Network`（Task 3）。
- Produces: `Rule34Adapter(name="rule34")`。凭证来自 config `[sites.rule34]` 的 `user_id` / `api_key`；缺失时 `search` / `get_post` 抛 `AuthError`。

- [ ] **Step 1: 写失败测试**

`tests/test_rule34.py`：

```python
"""rule34 适配器测试。"""
import httpx
import pytest
import respx

from media_mcp.config import Config, NetworkConfig
from media_mcp.models import AuthError, MediaType
from media_mcp.network import Network
from media_mcp.sites.rule34 import Rule34Adapter

RULE34_POST = {
    "id": 456,
    "tags": "tag1 tag2",
    "owner": "someone",
    "file_url": "https://wimg.rule34.xxx/images/abcd/a.mp4",
    "preview_url": "https://rule34.xxx/thumbnails/abcd/thumb.jpg",
    "score": 15,
    "rating": "explicit",
    "md5": "0123456789abcdef0123456789abcdef",
}


def _adapter(**sites):
    config = Config(network=NetworkConfig(retries=1), sites=sites)
    network = Network(config)
    return Rule34Adapter(config, network), network


async def test_missing_credentials_raises_auth():
    adapter, network = _adapter()
    with pytest.raises(AuthError):
        await adapter.search("tag1")
    await network.close()


async def test_search_builds_params_and_parses():
    adapter, network = _adapter(rule34={"user_id": "1", "api_key": "k"})
    with respx.mock:
        route = respx.get("https://api.rule34.xxx/index.php").mock(
            return_value=httpx.Response(200, json=[RULE34_POST])
        )
        posts = await adapter.search("tag1", limit=3, page=2)
        params = route.calls.last.request.url.params
        assert params["user_id"] == "1"
        assert params["api_key"] == "k"
        assert params["pid"] == "1"
        assert params["json"] == "1"
        assert params["tags"] == "tag1"
    post = posts[0]
    assert post.media_type == MediaType.VIDEO
    assert post.rating == "explicit"
    assert post.score == 15
    assert post.tags == ["tag1", "tag2"]
    await network.close()


async def test_targets_and_parse_url():
    adapter, _ = _adapter(rule34={"user_id": "1", "api_key": "k"})
    post = Rule34Adapter._to_post(RULE34_POST)
    (target,) = await adapter.get_download_targets(post)
    assert target.filename == "456_01234567.mp4"
    assert adapter.parse_url(
        "https://rule34.xxx/index.php?page=post&s=view&id=456"
    ) == "456"
    assert adapter.parse_url("https://e621.net/posts/1") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_rule34.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'media_mcp.sites.rule34'`）

- [ ] **Step 3: 实现 rule34.py**

`src/media_mcp/sites/rule34.py`：

```python
"""rule34.xxx 适配器：dapi JSON API（2025 年起强制 user_id + api_key）。"""
from __future__ import annotations

import re

from ..models import (
    AuthError,
    DownloadTarget,
    MediaType,
    NotFoundError,
    Post,
    SiteNetworkError,
)
from .base import SiteAdapter

VIDEO_EXTS = ("webm", "mp4")


class Rule34Adapter(SiteAdapter):
    name = "rule34"
    BASE = "https://api.rule34.xxx/index.php"

    def _credentials(self) -> dict[str, str]:
        user_id = str(self._config.site_get("rule34", "user_id", "") or "")
        api_key = str(self._config.site_get("rule34", "api_key", "") or "")
        if not user_id or not api_key:
            raise AuthError(
                "rule34.xxx API 需要 user_id 和 api_key",
                hint="注册 rule34.xxx 账号后在 Options 页面生成 API key，填入 config.toml 的 [sites.rule34]",
            )
        return {"user_id": user_id, "api_key": api_key}

    async def _fetch(self, params: dict) -> list[dict]:
        query = {
            "page": "dapi", "s": "post", "q": "index", "json": "1",
            **params, **self._credentials(),
        }
        response = await self._network.request(self.name, "GET", self.BASE, params=query)
        if response.status_code in (401, 403):
            raise AuthError("rule34 API 认证失败", hint="检查 [sites.rule34] 的 user_id/api_key")
        if response.status_code != 200:
            raise SiteNetworkError(f"rule34 返回 HTTP {response.status_code}")
        data = response.json()
        return data if isinstance(data, list) else []

    async def search(self, query, limit=20, page=1, min_score=None, rating=None):
        tags = query.strip()
        if rating:
            tags += f" rating:{rating}"
        if min_score is not None:
            tags += f" score:>={min_score}"
        raw = await self._fetch({"tags": tags.strip(), "limit": limit, "pid": page - 1})
        return [self._to_post(p) for p in raw]

    async def get_post(self, post_id: str) -> Post:
        raw = await self._fetch({"id": post_id, "limit": 1})
        if not raw:
            raise NotFoundError(f"rule34 找不到作品 {post_id}")
        return self._to_post(raw[0])

    async def get_download_targets(self, post: Post) -> list[DownloadTarget]:
        if not post.file_url:
            raise NotFoundError(f"rule34 作品 {post.id} 无文件直链")
        md5 = str(post.extra.get("md5", ""))[:8]
        ext = post.file_url.rsplit(".", 1)[-1].split("?")[0] or "jpg"
        return [DownloadTarget(url=post.file_url, filename=f"{post.id}_{md5}.{ext}")]

    def parse_url(self, url: str) -> str | None:
        match = re.search(r"rule34\.xxx/.*[?&]id=(\d+)", url)
        return match.group(1) if match else None

    async def check(self) -> dict:
        try:
            await self._fetch({"limit": 1})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "detail": "dapi 可访问且凭证有效"}

    @staticmethod
    def _to_post(raw: dict) -> Post:
        file_url = str(raw.get("file_url", "") or "")
        ext = file_url.rsplit(".", 1)[-1].split("?")[0].lower() if file_url else ""
        return Post(
            site="rule34",
            id=str(raw.get("id", "")),
            url=f"https://rule34.xxx/index.php?page=post&s=view&id={raw.get('id', '')}",
            tags=str(raw.get("tags", "")).split(),
            artist=[],
            rating=str(raw.get("rating", "")),
            score=raw.get("score"),
            media_type=MediaType.VIDEO if ext in VIDEO_EXTS else MediaType.IMAGE,
            file_url=file_url or None,
            preview_url=raw.get("preview_url") or None,
            extra={"md5": raw.get("md5", ""), "owner": raw.get("owner", "")},
        )
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_rule34.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```powershell
git add src/media_mcp/sites/rule34.py tests/test_rule34.py
git commit -m "feat: rule34 适配器（dapi + user_id/api_key 认证）"
```

---

### Task 7: E-Hentai / ExHentai 适配器

**Files:**
- Create: `src/media_mcp/sites/ehentai.py`
- Test: `tests/test_ehentai.py`

**Interfaces:**
- Consumes: `SiteAdapter`（Task 5）、模型与错误（Task 2）、`Network`（Task 3）。
- Produces: `EHentaiAdapter(name="ehentai")`。`post_id` 格式为 `"gid/token"`；`search` 用网页搜索页解析出 gid/token 列表后调官方 `gdata` API 补全元数据；`get_download_targets` 解析画廊页 → 逐图片页解析原图 URL；识别 509 配额与 Sad Panda。

- [ ] **Step 1: 写失败测试**

`tests/test_ehentai.py`：

```python
"""E-Hentai 适配器测试。"""
import httpx
import pytest
import respx

from media_mcp.config import Config, NetworkConfig
from media_mcp.models import AuthError, MediaType, QuotaError
from media_mcp.network import Network
from media_mcp.sites.ehentai import EHentaiAdapter

SEARCH_HTML = """
<html><body><table class="itg">
<tr><td><a href="https://e-hentai.org/g/12345/abc123def4/"><img src="t1.jpg"/></a></td></tr>
<tr><td><a href="https://e-hentai.org/g/12345/abc123def4/">dup</a></td></tr>
<tr><td><a href="https://e-hentai.org/g/67890/feedbeef99/"><img src="t2.jpg"/></a></td></tr>
</table></body></html>
"""

GDATA_RESPONSE = {
    "gmetadata": [
        {
            "gid": 12345,
            "token": "abc123def4",
            "title": "Test Gallery",
            "category": "Manga",
            "rating": "4.50",
            "tags": ["artist:someone", "language:chinese"],
            "filecount": "3",
            "thumb": "https://ehgt.org/t.jpg",
            "posted": "1700000000",
        }
    ]
}

IMAGE_PAGE_HTML = (
    '<html><body><div id="i3">'
    '<img id="img" src="https://ehgt.org/img/abc/full.jpg" />'
    "</div></body></html>"
)


def _adapter(**sites):
    config = Config(network=NetworkConfig(retries=1), sites=sites)
    network = Network(config)
    return EHentaiAdapter(config, network), network


def test_parse_search_html_dedup_and_order():
    assert EHentaiAdapter._parse_search_html(SEARCH_HTML) == [
        ["12345", "abc123def4"],
        ["67890", "feedbeef99"],
    ]


async def test_gdata_meta_to_post():
    adapter, network = _adapter()
    with respx.mock:
        route = respx.post("https://api.e-hentai.org/api.php").mock(
            return_value=httpx.Response(200, json=GDATA_RESPONSE)
        )
        posts = await adapter._gdata([["12345", "abc123def4"]])
        import json as _json
        body = _json.loads(route.calls.last.request.content)
        assert body["method"] == "gdata"
        assert body["gidlist"] == [[12345, "abc123def4"]]
    post = posts[0]
    assert post.id == "12345/abc123def4"
    assert post.media_type == MediaType.GALLERY
    assert post.page_count == 3
    assert post.artist == ["someone"]
    assert post.score == 4.5
    assert post.rating == "Manga"
    await network.close()


async def test_resolve_image_url():
    adapter, network = _adapter()
    with respx.mock:
        respx.get("https://e-hentai.org/s/pt/12345-1").mock(
            return_value=httpx.Response(200, text=IMAGE_PAGE_HTML)
        )
        url = await adapter._resolve_image_url("https://e-hentai.org/s/pt/12345-1")
    assert url == "https://ehgt.org/img/abc/full.jpg"
    await network.close()


async def test_resolve_quota_509():
    adapter, network = _adapter()
    with respx.mock:
        respx.get("https://e-hentai.org/s/pt/12345-1").mock(
            return_value=httpx.Response(
                200, text='<img id="img" src="https://e-hentai.org/img/509.gif" />'
            )
        )
        with pytest.raises(QuotaError):
            await adapter._resolve_image_url("https://e-hentai.org/s/pt/12345-1")
    await network.close()


async def test_sad_panda_raises_auth():
    adapter, network = _adapter(
        ehentai={"use_exhentai": True, "cookie": "ipb_member_id=1; ipb_pass_hash=x"}
    )
    with respx.mock:
        respx.get("https://exhentai.org/").mock(
            return_value=httpx.Response(
                200, content=b"GIF89a....", headers={"content-type": "image/gif"}
            )
        )
        with pytest.raises(AuthError):
            await adapter._get_text("https://exhentai.org/")
    await network.close()


def test_parse_url():
    adapter, _ = _adapter()
    assert adapter.parse_url("https://e-hentai.org/g/12345/abc123def4/") == "12345/abc123def4"
    assert adapter.parse_url("https://exhentai.org/g/12345/abc123def4/") == "12345/abc123def4"
    assert adapter.parse_url("https://e621.net/posts/1") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_ehentai.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'media_mcp.sites.ehentai'`）

- [ ] **Step 3: 实现 ehentai.py**

`src/media_mcp/sites/ehentai.py`：

```python
"""E-Hentai / ExHentai 适配器：网页搜索 + api.php 元数据 + 画廊页解析下载。"""
from __future__ import annotations

import asyncio
import re
from math import ceil

from bs4 import BeautifulSoup

from ..models import (
    AuthError,
    DownloadTarget,
    MediaType,
    NotFoundError,
    Post,
    QuotaError,
    SiteNetworkError,
)
from .base import SiteAdapter

GALLERY_RE = re.compile(r"/g/(\d+)/([0-9a-f]+)/?")
PAGE_RE = re.compile(r"/s/[0-9a-f]+/\d+-\d+")
THUMBS_PER_PAGE = 40  # 画廊页默认缩略图数
IMG_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}


class EHentaiAdapter(SiteAdapter):
    name = "ehentai"

    # ---- 基础设施 ----

    def _cookie(self) -> str:
        return str(self._config.site_get("ehentai", "cookie", "") or "")

    def _base(self) -> str:
        mirror = str(self._config.site_get("ehentai", "mirror_base", "") or "")
        if mirror:
            return mirror.rstrip("/")
        if self._config.site_get("ehentai", "use_exhentai", False) and self._cookie():
            return "https://exhentai.org"
        return "https://e-hentai.org"

    def _api_url(self) -> str:
        mirror = str(self._config.site_get("ehentai", "mirror_base", "") or "")
        if mirror:
            return f"{mirror.rstrip('/')}/api.php"
        if self._base().endswith("exhentai.org"):
            return "https://exhentai.org/api.php"
        return "https://api.e-hentai.org/api.php"

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "media-hunter-mcp/0.1 (gallery metadata client)"}
        cookie = self._cookie()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _get_text(self, url: str, params: dict | None = None) -> str:
        response = await self._network.request(
            self.name, "GET", url, params=params, headers=self._headers()
        )
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("image/"):
            raise AuthError(
                "ExHentai 返回 Sad Panda（Cookie 无效或未登录）",
                hint="更新 [sites.ehentai] cookie（ipb_member_id / ipb_pass_hash / igneous）",
            )
        if response.status_code in (401, 403):
            raise AuthError("E-Hentai 拒绝访问", hint="检查 [sites.ehentai] cookie 配置")
        if response.status_code == 404:
            raise NotFoundError(f"页面不存在: {url}")
        if response.status_code != 200:
            raise SiteNetworkError(f"E-Hentai 返回 HTTP {response.status_code}: {url}")
        return response.text

    # ---- 搜索与元数据 ----

    @staticmethod
    def _parse_search_html(html: str) -> list[list[str]]:
        """从搜索结果页提取 [gid, token] 列表，去重保序。"""
        seen: set[str] = set()
        gidlist: list[list[str]] = []
        for gid, token in GALLERY_RE.findall(html):
            if gid not in seen:
                seen.add(gid)
                gidlist.append([gid, token])
        return gidlist

    async def search(self, query, limit=20, page=1, min_score=None, rating=None):
        html = await self._get_text(
            f"{self._base()}/", params={"f_search": query, "page": page - 1}
        )
        gidlist = self._parse_search_html(html)[:limit]
        if not gidlist:
            return []
        posts = await self._gdata(gidlist)
        if min_score is not None:
            posts = [p for p in posts if (p.score or 0) >= min_score]
        if rating:
            posts = [p for p in posts if rating.lower() in p.rating.lower()]
        return posts

    async def _gdata(self, gidlist: list[list[str]]) -> list[Post]:
        """官方 gdata API 批量取元数据，25 条/请求，连发间歇 5 秒。"""
        posts: list[Post] = []
        for i in range(0, len(gidlist), 25):
            chunk = gidlist[i : i + 25]
            response = await self._network.request(
                self.name, "POST", self._api_url(),
                json={"method": "gdata", "gidlist": [[int(g), t] for g, t in chunk], "namespace": 1},
                headers=self._headers(),
            )
            if response.status_code != 200:
                raise SiteNetworkError(f"E-Hentai API 返回 HTTP {response.status_code}")
            for meta in response.json().get("gmetadata", []):
                if "error" not in meta:
                    posts.append(self._meta_to_post(meta))
            if i + 25 < len(gidlist):
                await asyncio.sleep(5)
        return posts

    def _meta_to_post(self, meta: dict) -> Post:
        gid = str(meta.get("gid", ""))
        token = str(meta.get("token", ""))
        try:
            score = float(meta.get("rating", 0) or 0)
        except (TypeError, ValueError):
            score = None
        return Post(
            site=self.name,
            id=f"{gid}/{token}",
            url=f"{self._base()}/g/{gid}/{token}/",
            title=str(meta.get("title", "")),
            tags=list(meta.get("tags", [])),
            artist=[t.split(":", 1)[1] for t in meta.get("tags", []) if t.startswith("artist:")],
            rating=str(meta.get("category", "")),
            score=score,
            media_type=MediaType.GALLERY,
            preview_url=str(meta.get("thumb", "")) or None,
            page_count=int(meta.get("filecount", 0) or 0),
            extra={"gid": gid, "token": token, "posted": meta.get("posted", "")},
        )

    async def get_post(self, post_id: str) -> Post:
        parts = post_id.split("/")
        if len(parts) != 2:
            raise NotFoundError(
                f"ehentai 的 post_id 格式应为 gid/token，收到: {post_id}",
            )
        posts = await self._gdata([[parts[0], parts[1]]])
        if not posts:
            raise NotFoundError(f"E-Hentai 找不到画廊 {post_id}")
        return posts[0]

    # ---- 下载目标解析 ----

    async def get_download_targets(self, post: Post) -> list[DownloadTarget]:
        gid = str(post.extra.get("gid") or post.id.split("/")[0])
        token = str(post.extra.get("token") or post.id.split("/")[1])
        page_urls = await self._collect_page_urls(gid, token, post.page_count)
        targets: list[DownloadTarget] = []
        for page_no, page_url in enumerate(page_urls, start=1):
            image_url = await self._resolve_image_url(page_url)
            ext = image_url.rsplit(".", 1)[-1].split("?")[0].lower()
            if ext not in IMG_EXTS:
                ext = "jpg"
            targets.append(
                DownloadTarget(
                    url=image_url,
                    filename=f"{page_no:03d}.{ext}",
                    page=page_no,
                    headers={"Referer": page_url},
                )
            )
        return targets

    async def _collect_page_urls(self, gid: str, token: str, page_count: int) -> list[str]:
        urls: list[str] = []
        thumb_pages = max(1, ceil(page_count / THUMBS_PER_PAGE))
        for p in range(thumb_pages):
            html = await self._get_text(
                f"{self._base()}/g/{gid}/{token}/", params={"p": p}
            )
            for rel in dict.fromkeys(PAGE_RE.findall(html)):
                urls.append(f"{self._base()}{rel}")
        return urls[:page_count] if page_count else urls

    async def _resolve_image_url(self, page_url: str) -> str:
        html = await self._get_text(page_url)
        soup = BeautifulSoup(html, "html.parser")
        img = soup.find("img", id="img")
        src = str(img.get("src", "")) if img else ""
        if not src:
            if "exceeded your image limits" in html.lower():
                raise QuotaError(
                    "E-Hentai 图片配额已用尽",
                    hint="等待配额恢复，或在 config.toml 配置 mirror_base / 更换账号",
                )
            raise NotFoundError(f"无法解析图片页: {page_url}")
        if "/509" in src:
            raise QuotaError(
                "E-Hentai 图片配额已用尽",
                hint="等待配额恢复，或配置镜像",
            )
        return src

    # ---- 其他 ----

    def parse_url(self, url: str) -> str | None:
        match = re.search(r"(?:e-hentai|exhentai)\.org/g/(\d+)/([0-9a-f]+)", url)
        return f"{match.group(1)}/{match.group(2)}" if match else None

    async def check(self) -> dict:
        try:
            await self._get_text(f"{self._base()}/")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "detail": f"{self._base()} 可访问"}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_ehentai.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```powershell
git add src/media_mcp/sites/ehentai.py tests/test_ehentai.py
git commit -m "feat: E-Hentai/ExHentai 适配器（搜索+gdata+画廊页解析+配额感知）"
```

---

### Task 8: pixiv 适配器

**Files:**
- Create: `src/media_mcp/sites/pixiv.py`
- Test: `tests/test_pixiv.py`

**Interfaces:**
- Consumes: `SiteAdapter`（Task 5）、模型与错误（Task 2）、`Network`（Task 3，仅用于网络层一致性，pixiv 请求走 pixivpy3 自带 session + 代理）。
- Produces: `PixivAdapter(name="pixiv")`；pixivpy3 的 `AppPixivAPI` 实例缓存于 `self._api`（测试可直接注入 `adapter._api = <fake>` 绕过认证）；`rating` 参数取值 `all` / `r18` / `r18g`（映射 `x_restrict` 0/1/2）。

- [ ] **Step 1: 写失败测试**

`tests/test_pixiv.py`：

```python
"""pixiv 适配器测试（pixivpy3 全部 mock，不触网）。"""
from types import SimpleNamespace as NS

import pytest

from media_mcp.config import Config
from media_mcp.models import AuthError, MediaType, Post
from media_mcp.sites.pixiv import PixivAdapter

ILLUST = NS(
    id=777, title="测试图", type="illust", page_count=2, x_restrict=1,
    total_bookmarks=99, tags=[NS(name="tagA"), NS(name="tagB")],
    user=NS(id=8, name="画师B"),
    image_urls=NS(medium="https://i.pximg.net/m.jpg"),
    meta_single_page={},
    meta_pages=[
        NS(image_urls=NS(original="https://i.pximg.net/p0.png")),
        NS(image_urls=NS(original="https://i.pximg.net/p1.png")),
    ],
)


def test_to_post_multi_page():
    post = PixivAdapter._to_post(ILLUST)
    assert post.id == "777"
    assert post.media_type == MediaType.GALLERY
    assert post.page_count == 2
    assert post.rating == "R-18"
    assert post.artist == ["画师B"]
    assert post.score == 99
    assert post.extra["user_id"] == 8


async def test_targets_multi_page_with_referer():
    adapter = PixivAdapter(Config(), None)
    adapter._api = NS(illust_detail=lambda illust_id: NS(illust=ILLUST))
    post = PixivAdapter._to_post(ILLUST)
    targets = await adapter.get_download_targets(post)
    assert [t.filename for t in targets] == ["777_p0.png", "777_p1.png"]
    assert all(t.headers["Referer"] == "https://pixiv.net" for t in targets)


async def test_ugoira_target():
    meta = NS(
        ugoira_metadata=NS(
            zip_urls=NS(original="https://i.pximg.net/u.zip"),
            frames=[NS(delay=100), NS(delay=120)],
        )
    )
    adapter = PixivAdapter(Config(), None)
    adapter._api = NS(ugoira_metadata=lambda illust_id: meta)
    post = Post(site="pixiv", id="778", url="u", media_type=MediaType.UGOIRA)
    (target,) = await adapter.get_download_targets(post)
    assert target.filename == "778_ugoira.zip"
    assert target.post_process == "ugoira"
    assert target.post_process_meta["delays"] == [100, 120]


def test_image_mirror_rewrite():
    adapter = PixivAdapter(Config(sites={"pixiv": {"image_mirror": "https://i.pixiv.re"}}), None)
    assert adapter._rewrite_image_url(
        "https://i.pximg.net/img-original/img/a.jpg"
    ) == "https://i.pixiv.re/img-original/img/a.jpg"


def test_missing_refresh_token_raises_auth():
    adapter = PixivAdapter(Config(), None)
    with pytest.raises(AuthError):
        adapter._client()


def test_parse_url():
    adapter = PixivAdapter(Config(), None)
    assert adapter.parse_url("https://www.pixiv.net/artworks/777") == "777"
    assert adapter.parse_url("https://www.pixiv.net/en/artworks/777") == "777"
    assert adapter.parse_url("https://www.pixiv.net/member_illust.php?illust_id=777&mode=medium") == "777"
    assert adapter.parse_url("https://e621.net/posts/1") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_pixiv.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'media_mcp.sites.pixiv'`）

- [ ] **Step 3: 实现 pixiv.py**

`src/media_mcp/sites/pixiv.py`：

```python
"""pixiv 适配器：pixivpy3 app-api 封装（同步库，一律 asyncio.to_thread 调用）。"""
from __future__ import annotations

import asyncio
import re

from pixivpy3 import AppPixivAPI

from ..models import (
    AuthError,
    DownloadTarget,
    MediaType,
    NotFoundError,
    Post,
    QuotaError,
    SiteNetworkError,
    MediaMcpError,
)
from .base import SiteAdapter

X_RESTRICT_MAP = {"all": 0, "r18": 1, "r18g": 2}
REFERER = "https://pixiv.net"


class PixivAdapter(SiteAdapter):
    name = "pixiv"

    def __init__(self, config, network):
        super().__init__(config, network)
        self._api: AppPixivAPI | None = None

    # ---- 认证 ----

    def _client(self) -> AppPixivAPI:
        if self._api is None:
            refresh_token = str(self._config.site_get("pixiv", "refresh_token", "") or "")
            if not refresh_token:
                raise AuthError(
                    "缺少 pixiv refresh_token",
                    hint="用 gppt（pip install gppt）获取 refresh_token，填入 config.toml 的 [sites.pixiv]",
                )
            api = AppPixivAPI()
            proxy = self._config.network.proxy
            if proxy:
                api.requests.proxies = {"http": proxy, "https": proxy}
            api_base = str(self._config.site_get("pixiv", "api_base", "") or "")
            if api_base:
                api.hosts = api_base.rstrip("/")
            try:
                api.auth(refresh_token=refresh_token)
            except Exception as exc:
                raise AuthError(
                    f"pixiv 认证失败: {exc}",
                    hint="refresh_token 可能已过期，请用 gppt 重新获取",
                ) from exc
            self._api = api
        return self._api

    @staticmethod
    def _translate_error(exc: Exception) -> MediaMcpError:
        text = str(exc).lower()
        if "rate limit" in text or "429" in text:
            return QuotaError("pixiv 触发限流", hint="稍后重试或降低请求频率")
        if "token" in text and ("invalid" in text or "expired" in text):
            return AuthError("pixiv token 失效", hint="重新获取 refresh_token")
        return SiteNetworkError(f"pixiv 请求失败: {exc}")

    # ---- 搜索与元数据 ----

    async def search(self, query, limit=20, page=1, min_score=None, rating=None):
        api = self._client()
        limit = min(limit, 30)  # app-api 单页最多 30
        try:
            result = await asyncio.to_thread(
                api.search_illust, query, sort="date_desc", offset=(page - 1) * 30
            )
        except Exception as exc:
            raise self._translate_error(exc) from exc
        illusts = getattr(result, "illusts", None) or []
        posts = [self._to_post(i) for i in illusts]
        if rating:
            threshold = X_RESTRICT_MAP.get(rating.lower())
            if threshold is not None:
                posts = [p for p in posts if int(p.extra.get("x_restrict", 0)) >= threshold]
        if min_score is not None:
            posts = [p for p in posts if (p.score or 0) >= min_score]
        return posts[:limit]

    async def get_post(self, post_id: str) -> Post:
        api = self._client()
        try:
            result = await asyncio.to_thread(api.illust_detail, int(post_id))
        except Exception as exc:
            raise self._translate_error(exc) from exc
        illust = getattr(result, "illust", None)
        if illust is None:
            raise NotFoundError(f"pixiv 找不到作品 {post_id}")
        return self._to_post(illust)

    @staticmethod
    def _to_post(illust) -> Post:
        user = getattr(illust, "user", None)
        illust_type = getattr(illust, "type", "illust") or "illust"
        page_count = int(getattr(illust, "page_count", 1) or 1)
        x_restrict = int(getattr(illust, "x_restrict", 0) or 0)
        if illust_type == "ugoira":
            media_type = MediaType.UGOIRA
        elif page_count > 1:
            media_type = MediaType.GALLERY
        else:
            media_type = MediaType.IMAGE
        image_urls = getattr(illust, "image_urls", None)
        return Post(
            site="pixiv",
            id=str(illust.id),
            url=f"https://www.pixiv.net/artworks/{illust.id}",
            title=str(getattr(illust, "title", "") or ""),
            tags=[t.name for t in getattr(illust, "tags", []) or []],
            artist=[user.name] if user else [],
            rating="R-18" if x_restrict else "all",
            score=int(getattr(illust, "total_bookmarks", 0) or 0),
            media_type=media_type,
            page_count=page_count,
            preview_url=getattr(image_urls, "medium", None) if image_urls else None,
            extra={
                "x_restrict": x_restrict,
                "user_id": getattr(user, "id", 0) if user else 0,
                "illust_type": illust_type,
            },
        )

    # ---- 下载目标 ----

    def _rewrite_image_url(self, url: str) -> str:
        mirror = str(self._config.site_get("pixiv", "image_mirror", "") or "")
        if mirror:
            return re.sub(r"^https://i\.pximg\.net", mirror.rstrip("/"), url)
        return url

    async def get_download_targets(self, post: Post) -> list[DownloadTarget]:
        api = self._client()
        illust_id = int(post.id)
        if post.media_type == MediaType.UGOIRA:
            try:
                meta = await asyncio.to_thread(api.ugoira_metadata, illust_id)
            except Exception as exc:
                raise self._translate_error(exc) from exc
            ugoira = meta.ugoira_metadata
            delays = [f.delay for f in getattr(ugoira, "frames", []) or []]
            return [
                DownloadTarget(
                    url=self._rewrite_image_url(ugoira.zip_urls.original),
                    filename=f"{post.id}_ugoira.zip",
                    headers={"Referer": REFERER},
                    post_process="ugoira",
                    post_process_meta={"delays": delays},
                )
            ]
        try:
            detail = await asyncio.to_thread(api.illust_detail, illust_id)
        except Exception as exc:
            raise self._translate_error(exc) from exc
        illust = getattr(detail, "illust", None)
        if illust is None:
            raise NotFoundError(f"pixiv 找不到作品 {post.id}")
        pages = getattr(illust, "meta_pages", None) or []
        if pages:
            urls = [p.image_urls.original for p in pages]
        else:
            urls = [(getattr(illust, "meta_single_page", None) or {}).get("original_image_url")]
        targets: list[DownloadTarget] = []
        for i, url in enumerate(urls):
            if not url:
                continue
            ext = url.rsplit(".", 1)[-1]
            targets.append(
                DownloadTarget(
                    url=self._rewrite_image_url(url),
                    filename=f"{post.id}_p{i}.{ext}",
                    page=i,
                    headers={"Referer": REFERER},
                )
            )
        if not targets:
            raise NotFoundError(f"pixiv 作品 {post.id} 无可下载文件")
        return targets

    # ---- 其他 ----

    def parse_url(self, url: str) -> str | None:
        match = re.search(r"pixiv\.net/(?:[a-z]{2}/)?artworks/(\d+)", url)
        if match:
            return match.group(1)
        match = re.search(r"pixiv\.net/.*[?&]illust_id=(\d+)", url)
        return match.group(1) if match else None

    async def check(self) -> dict:
        try:
            api = self._client()
            await asyncio.to_thread(api.illust_detail, 99549921)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "detail": "refresh_token 有效，API 可访问"}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_pixiv.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```powershell
git add src/media_mcp/sites/pixiv.py tests/test_pixiv.py
git commit -m "feat: pixiv 适配器（pixivpy3 + refresh_token + ugoira 目标）"
```

---

### Task 9: FastMCP 服务器（6 个工具）

**Files:**
- Create: `src/media_mcp/server.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: 全部前序任务。`SiteAdapter`（Task 5）、`Downloader.download`（Task 4）、`Post / DownloadResult / MediaMcpError`（Task 2）。
- Produces: 模块级 `_ADAPTERS: dict[str, SiteAdapter]`、`_downloader`、`_config`；6 个实现函数 `_search_impl / _get_post_impl / _download_post_impl / _download_search_impl / _download_url_impl / _self_check_impl`（测试直接调这些，避免依赖 FastMCP 装饰器返回值语义）；`main()` 入口（`mcp.run()`）。

- [ ] **Step 1: 写失败测试**

`tests/test_server.py`：

```python
"""服务器工具层测试：信封、路由、错误映射。"""
from pathlib import Path

import pytest

from media_mcp import server
from media_mcp.models import AuthError, DownloadResult, DownloadTarget, Post


class FakeAdapter:
    name = "fake"

    def __init__(self, post: Post | None = None):
        self.post = post or Post(site="fake", id="1", url="https://fake.example/1")

    async def search(self, query, limit=20, page=1, min_score=None, rating=None):
        return [self.post]

    async def get_post(self, post_id):
        if post_id == "missing":
            raise AuthError("凭证失效", hint="去配置里更新")
        return self.post

    async def get_download_targets(self, post):
        return [DownloadTarget(url="https://fake.example/f.jpg", filename="1_f.jpg")]

    def parse_url(self, url):
        return "1" if "fake.example" in url else None

    async def check(self):
        return {"ok": True, "detail": "fake ok"}


class FakeDownloader:
    def __init__(self, root: Path):
        self.root = root

    async def download(self, post, targets, subdir=None):
        return DownloadResult(
            post=post, directory=str(self.root), files=[], sidecar_path=str(self.root / "1.json")
        )


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    adapter = FakeAdapter()
    # 整体替换适配器表，避免 self_check 等遍历到真实适配器触网
    monkeypatch.setattr(server, "_ADAPTERS", {"fake": adapter})
    monkeypatch.setattr(server, "_downloader", FakeDownloader(tmp_path))
    return adapter


async def test_search_envelope(fake_env):
    result = await server._search_impl("fake", "q", 20, 1, None, None)
    assert result["success"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["posts"][0]["id"] == "1"


async def test_unknown_site_error():
    result = await server._search_impl("nope", "q", 20, 1, None, None)
    assert result["success"] is False
    assert "未知站点" in result["error"]["message"]


async def test_auth_error_envelope(fake_env):
    result = await server._get_post_impl("fake", "missing")
    assert result["success"] is False
    assert result["error"]["type"] == "auth"
    assert result["error"]["hint"] == "去配置里更新"


async def test_download_url_routes_to_adapter(fake_env):
    result = await server._download_url_impl("https://fake.example/1", None)
    assert result["success"] is True
    assert result["data"]["post"]["id"] == "1"


async def test_download_url_unrecognized():
    result = await server._download_url_impl("https://unknown.example/x", None)
    assert result["success"] is False
    assert "无法识别" in result["error"]["message"]


async def test_download_search_aggregates(fake_env):
    result = await server._download_search_impl("fake", "q", 5, None, None, None)
    assert result["success"] is True
    assert len(result["data"]["downloaded"]) == 1
    assert result["data"]["errors"] == []


async def test_self_check_includes_adapter(fake_env):
    result = await server._self_check_impl()
    assert result["success"] is True
    assert result["data"]["fake"] == {"ok": True, "detail": "fake ok"}
    assert "proxy" in result["data"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_server.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'media_mcp.server'`）

- [ ] **Step 3: 实现 server.py**

`src/media_mcp/server.py`：

```python
"""FastMCP 入口：注册 6 个工具。核心逻辑在 *_impl 函数中，便于测试。"""
from __future__ import annotations

from fastmcp import FastMCP

from .config import load_config
from .downloader import Downloader
from .models import MediaMcpError, Post
from .network import Network
from .sites.base import SiteAdapter
from .sites.e621 import E621Adapter
from .sites.ehentai import EHentaiAdapter
from .sites.pixiv import PixivAdapter
from .sites.rule34 import Rule34Adapter

mcp = FastMCP("media-hunter-mcp")

_config = load_config()
_network = Network(_config)
_downloader = Downloader(_config, _network)
_ADAPTERS: dict[str, SiteAdapter] = {
    a.name: a
    for a in (
        E621Adapter(_config, _network),
        Rule34Adapter(_config, _network),
        EHentaiAdapter(_config, _network),
        PixivAdapter(_config, _network),
    )
}

MAX_BATCH_FILES = 50


def _ok(data) -> dict:
    return {"success": True, "data": data}


def _fail(exc: Exception) -> dict:
    if isinstance(exc, MediaMcpError):
        return {"success": False, "error": exc.to_dict()}
    return {"success": False, "error": {"type": "internal", "message": str(exc), "hint": ""}}


def _adapter(site: str) -> SiteAdapter:
    adapter = _ADAPTERS.get(site.lower())
    if adapter is None:
        raise MediaMcpError(f"未知站点: {site}", hint=f"可用: {', '.join(sorted(_ADAPTERS))}")
    return adapter


async def _search_impl(site, query, limit, page, min_score, rating) -> dict:
    try:
        posts = await _adapter(site).search(
            query, limit=limit, page=page, min_score=min_score, rating=rating
        )
        return _ok({"count": len(posts), "posts": [p.to_dict() for p in posts]})
    except Exception as exc:
        return _fail(exc)


async def _get_post_impl(site, post_id) -> dict:
    try:
        post = await _adapter(site).get_post(post_id)
        return _ok(post.to_dict())
    except Exception as exc:
        return _fail(exc)


async def _download(post: Post, subdir: str | None) -> dict:
    targets = await _ADAPTERS[post.site].get_download_targets(post)
    result = await _downloader.download(post, targets, subdir=subdir)
    return result.to_dict()


async def _download_post_impl(site, post_id, subdir) -> dict:
    try:
        adapter = _adapter(site)
        post = await adapter.get_post(post_id)
        return _ok(await _download(post, subdir))
    except Exception as exc:
        return _fail(exc)


async def _download_search_impl(site, query, limit, min_score, rating, subdir) -> dict:
    try:
        adapter = _adapter(site)
        posts = await adapter.search(query, limit=limit, min_score=min_score, rating=rating)
        results, errors, total = [], [], 0
        for post in posts:
            if total >= MAX_BATCH_FILES:
                break
            try:
                data = await _download(post, subdir)
                total += len(data["files"])
                results.append(
                    {"id": post.id, "directory": data["directory"], "files": len(data["files"])}
                )
            except Exception as exc:  # 单个失败不阻断整批
                errors.append({"id": post.id, "error": str(exc)})
        return _ok({"downloaded": results, "errors": errors, "total_files": total})
    except Exception as exc:
        return _fail(exc)


async def _download_url_impl(url, subdir) -> dict:
    for adapter in _ADAPTERS.values():
        post_id = adapter.parse_url(url)
        if post_id:
            try:
                post = await adapter.get_post(post_id)
                return _ok(await _download(post, subdir))
            except Exception as exc:
                return _fail(exc)
    return _fail(
        MediaMcpError(
            f"无法识别的 URL: {url}",
            hint="支持 e621.net / rule34.xxx / e-hentai.org / exhentai.org / pixiv.net 的作品链接",
        )
    )


async def _self_check_impl() -> dict:
    report: dict = {}
    for name, adapter in _ADAPTERS.items():
        try:
            report[name] = await adapter.check()
        except Exception as exc:
            report[name] = {"ok": False, "detail": str(exc)}
    report["download_root"] = str(_config.download_root)
    report["proxy"] = _config.network.proxy or "(直连)"
    return _ok(report)


@mcp.tool
async def search(
    site: str, query: str, limit: int = 20, page: int = 1,
    min_score: float | None = None, rating: str | None = None,
) -> dict:
    """在 e621 / rule34 / ehentai / pixiv 搜索资源（不下载）。

    query 用各站原生语法（如 e621/rule34 的标签语法、E-Hentai 的 f_search、pixiv 关键词）。
    rating 语义：e621/rule34 为 s/q/e（或 safe/questionable/explicit），ehentai 匹配分类
    （如 manga、doujinshi），pixiv 为 all/r18/r18g。
    """
    return await _search_impl(site, query, limit, page, min_score, rating)


@mcp.tool
async def get_post(site: str, post_id: str) -> dict:
    """获取单个作品/画廊完整元数据。ehentai 的 post_id 格式为 "gid/token"。"""
    return await _get_post_impl(site, post_id)


@mcp.tool
async def download_post(site: str, post_id: str, subdir: str | None = None) -> dict:
    """按 ID 下载作品（画廊/多图整本下载），返回保存目录与文件清单。subdir 可指定子目录名。"""
    return await _download_post_impl(site, post_id, subdir)


@mcp.tool
async def download_search(
    site: str, query: str, limit: int = 10,
    min_score: float | None = None, rating: str | None = None, subdir: str | None = None,
) -> dict:
    """搜索并批量下载前 limit 个结果（最多 50 个文件），逐项报告成功/失败。"""
    return await _download_search_impl(site, query, limit, min_score, rating, subdir)


@mcp.tool
async def download_url(url: str, subdir: str | None = None) -> dict:
    """粘贴作品页面 URL，自动识别站点并下载。"""
    return await _download_url_impl(url, subdir)


@mcp.tool
async def self_check() -> dict:
    """检查各站点凭证、代理与镜像连通性，返回逐项报告。"""
    return await _self_check_impl()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_server.py -v`
Expected: 7 passed

- [ ] **Step 5: 验证服务器能启动导入**

Run: `uv run python -c "from media_mcp import server; print('tools ok:', len(server._ADAPTERS))"`
Expected: 输出 `tools ok: 4`

- [ ] **Step 6: Commit**

```powershell
git add src/media_mcp/server.py tests/test_server.py
git commit -m "feat: FastMCP 服务器（6 个工具 + 统一信封）"
```

---

### Task 10: README + 接入 OpenCode + 全量验证

**Files:**
- Create: `README.md`
- Modify: 无（OpenCode 配置由用户自己改 `~/.config/opencode/opencode.json`，README 给出片段）

**Interfaces:**
- Consumes: 全部。

- [ ] **Step 1: 写 README.md**

`README.md`：

````markdown
# media-hunter-mcp

多站点媒体搜索下载 MCP 服务器：让 AI 代理直接搜索并下载 **e621 / rule34.xxx / E-Hentai / pixiv** 的图片、画廊（漫画）、视频。

## 安装

```powershell
cd media-hunter-mcp
uv sync
```

## 配置

复制 `config.example.toml` 为 `config.toml`，按需填写：

- `download_root`：下载根目录（自动按站点分类）
- `[network] proxy`：代理端口（如 `http://127.0.0.1:7897`），留空则直连
- `[sites.e621]`：可选 username + api_key（提高限额）
- `[sites.rule34]`：**必填** user_id + api_key（注册后在 Options 页生成）
- `[sites.ehentai]`：cookie（解锁 ExHentai）、use_exhentai、mirror_base
- `[sites.pixiv]`：refresh_token（用 `pip install gppt && gppt login` 获取）、image_mirror

每个站点支持 `mirror_base`（反代镜像地址，镜像域名自行维护，不硬编码）。

## 工具

| 工具 | 说明 |
|------|------|
| `search` | 搜索，返回结构化元数据（站点原生查询语法） |
| `get_post` | 单个作品/画廊完整元数据 |
| `download_post` | 按 ID 下载（画廊整本） |
| `download_search` | 搜索并批量下载（≤50 文件） |
| `download_url` | 识别 URL 自动下载 |
| `self_check` | 凭证/代理/镜像连通性自检 |

## 接入 OpenCode

在 `%USERPROFILE%\.config\opencode\opencode.json` 的 `mcp` 段添加：

```json
"media-hunter": {
  "type": "local",
  "command": ["uv", "run", "--project", "C:\\Users\\x'w\\Desktop\\工作区\\media-hunter-mcp", "media-mcp"],
  "enabled": true,
  "environment": {
    "MEDIA_HUNTER_CONFIG": "C:\\Users\\x'w\\Desktop\\工作区\\media-hunter-mcp\\config.toml"
  }
}
```

重启 OpenCode 后即可使用，先跑 `self_check` 验证配置。

## 测试

```powershell
uv run pytest -v        # 全部单元测试（不触网）
```

## 注意

- E-Hentai 原图下载消耗每日图片配额，大量下载请调大 `[sites.ehentai] download_delay`
- ugoira 合成需要本机 ffmpeg（配置 `ugoira_format = "gif" | "mp4"`）
````

- [ ] **Step 2: 全量测试**

Run: `uv run pytest -v`
Expected: 全部通过（36 个测试）

- [ ] **Step 3: Commit**

```powershell
git add README.md
git commit -m "docs: README 与 OpenCode 接入说明"
```

---

## 交付清单核对（对应设计文档）

| 设计要求 | 实现任务 |
|---------|---------|
| pyproject / uv / FastMCP | Task 1 |
| config.toml + 环境变量 | Task 1 |
| Post 统一模型 + 错误信封 | Task 2 |
| 代理 + 镜像 fallback + 限速 | Task 3 |
| 下载引擎 + sidecar + ugoira | Task 4 |
| SiteAdapter 接口 | Task 5 |
| e621 / rule34 / ehentai / pixiv 适配器 | Task 5-8 |
| 6 个 MCP 工具 | Task 9 |
| README + OpenCode 接入 | Task 10 |



