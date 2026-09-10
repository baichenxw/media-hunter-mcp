"""站点适配器抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from urllib.parse import urlsplit

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

    def _url_parts(self, url, hosts):
        try:
            parts = urlsplit(url)
            if parts.scheme in {"http", "https"} and parts.hostname in hosts and not parts.username:
                return parts
        except ValueError:
            pass
        return None

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
