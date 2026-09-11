"""应用服务：站点路由、输入校验、时间预算与批量下载策略。"""

from __future__ import annotations

import asyncio
import math
import re
from urllib.parse import urlsplit

from .config import Config, load_config
from .downloader import Downloader
from .models import MediaMcpError, ToolTimeoutError, ValidationError
from .network import Network
from .progress import ProgressCallback, progress_scope, report_progress
from .sites.e621 import E621Adapter
from .sites.ehentai import EHentaiAdapter
from .sites.pixiv import PixivAdapter
from .sites.rule34 import Rule34Adapter

MAX_BATCH_FILES = 50
SITE_LIMITS = {"e621": 320, "rule34": 1000, "ehentai": 100, "pixiv": 30}
RATINGS = {
    "e621": {"s": "s", "q": "q", "e": "e", "safe": "s", "questionable": "q", "explicit": "e"},
    "rule34": {
        "s": "safe",
        "q": "questionable",
        "e": "explicit",
        "safe": "safe",
        "questionable": "questionable",
        "explicit": "explicit",
    },
    "pixiv": {
        "all": "all",
        "safe": "safe",
        "r18": "r18",
        "r18g": "r18g",
        "r-18": "r18",
        "r-18g": "r18g",
    },
}


class MediaService:
    def __init__(self, config: Config | None = None):
        self.config = config if config is not None else load_config()
        self.network = Network(self.config)
        self.downloader = Downloader(self.config, self.network)
        self.adapters = {
            a.name: a
            for a in (
                E621Adapter(self.config, self.network),
                Rule34Adapter(self.config, self.network),
                EHentaiAdapter(self.config, self.network),
                PixivAdapter(self.config, self.network),
            )
        }
        self._download_locks: dict[str, asyncio.Lock] = {}

    async def close(self):
        await self.network.close()

    def _redact(self, value):
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        if not isinstance(value, str):
            return value
        for values in self.config.sites.values():
            for key in ("api_key", "refresh_token", "cookie"):
                secret = str(values.get(key) or "")
                if secret:
                    value = value.replace(secret, "[REDACTED]")
        value = re.sub(
            r"(?i)(api_key|access_token|refresh_token|ipb_pass_hash|igneous)([=\s:]+)[^&\s;]+",
            r"\1\2[REDACTED]",
            value,
        )
        return value

    def _error(self, exc):
        if isinstance(exc, MediaMcpError):
            return self._redact(exc.to_dict())
        if isinstance(exc, OSError):
            return {
                "type": "filesystem",
                "message": "文件操作失败",
                "hint": "检查下载目录权限、剩余空间与路径长度",
            }
        return {
            "type": "internal",
            "message": f"操作失败（{type(exc).__name__}）",
            "hint": "检查站点响应或运行测试排查",
        }

    async def execute(self, operation, *, progress: ProgressCallback | None = None, **kwargs):
        with progress_scope(progress):
            result = await self._execute(operation, **kwargs)
            await report_progress("操作完成" if result["success"] else "操作结束，存在未完成项")
            return result

    async def _execute(self, operation, **kwargs):
        try:
            timeout = kwargs.pop("timeout", None)
            if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
                raise ValidationError("timeout 必须为正秒数，或省略表示不限总时长")
            if operation == "download_search":
                data = await self.download_search(timeout=timeout, **kwargs)
            else:
                async with asyncio.timeout(timeout):
                    data = await getattr(self, operation)(**kwargs)
            # 部分成功和全部失败都有完整文件清单，避免误报整本成功。
            complete = data.get("complete", True) if isinstance(data, dict) else True
            envelope = {"success": complete, "data": data}
            if not complete:
                envelope["error"] = {
                    "type": "partial_download",
                    "message": "部分文件或作品未完成，详见 data.errors",
                    "hint": "成功文件已保留",
                }
            return self._redact(envelope)
        except TimeoutError:
            return {
                "success": False,
                "error": ToolTimeoutError(
                    "时间预算已耗尽，任务已取消", "已完成文件保留；可增大 timeout 后重试"
                ).to_dict(),
            }
        except Exception as exc:
            return {"success": False, "error": self._error(exc)}

    def adapter(self, site, *, use_exhentai=None, original=False):
        name = site.strip().lower()
        if name not in self.adapters:
            raise ValidationError(f"未知站点: {name}", "可用: " + ", ".join(self.adapters))
        if use_exhentai is not None and not isinstance(use_exhentai, bool):
            raise ValidationError("use_exhentai 必须是布尔值")
        if not isinstance(original, bool):
            raise ValidationError("original 必须是布尔值")
        adapter = self.adapters[name]
        if name != "ehentai":
            if use_exhentai is not None or original:
                raise ValidationError("use_exhentai 和 original 仅适用于 ehentai")
        elif use_exhentai is not None or original:
            adapter = adapter.with_options(use_exhentai=use_exhentai, original=original)
        return adapter

    def _search_options(self, site, limit, page, min_score, rating):
        max_limit = SITE_LIMITS.get(site, 100)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= max_limit:
            raise ValidationError(f"{site} 的 limit 必须为 1–{max_limit}")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ValidationError("page 必须是从 1 开始的整数")
        if site == "ehentai" and page > 100:
            raise ValidationError("E-Hentai 顺序翻页最多支持 page=100，请缩小搜索范围")
        if min_score is not None and not math.isfinite(min_score):
            raise ValidationError("min_score 必须是有限数值")
        if rating:
            rating = rating.strip().lower()
            if site in RATINGS:
                if rating not in RATINGS[site]:
                    raise ValidationError(
                        f"{site} 不支持 rating={rating}", "可用: " + ", ".join(RATINGS[site])
                    )
                rating = RATINGS[site][rating]
        return dict(limit=limit, page=page, min_score=min_score, rating=rating)

    @staticmethod
    def _validate_id(site, post_id):
        pattern = r"\d+/[0-9a-f]+" if site == "ehentai" else r"\d+"
        if site in SITE_LIMITS and not re.fullmatch(pattern, post_id):
            raise ValidationError(
                "post_id 应为 gid/token" if site == "ehentai" else "post_id 必须是数字 ID"
            )

    async def search(
        self, site, query, limit=20, page=1, min_score=None, rating=None, use_exhentai=None
    ):
        adapter = self.adapter(site, use_exhentai=use_exhentai)
        options = self._search_options(adapter.name, limit, page, min_score, rating)
        posts = await adapter.search(query, **options)
        return {
            "count": len(posts),
            "page": page,
            "limit": limit,
            "posts": [p.to_dict() for p in posts],
        }

    async def get_post(self, site, post_id, use_exhentai=None):
        adapter = self.adapter(site, use_exhentai=use_exhentai)
        self._validate_id(adapter.name, post_id)
        return (await adapter.get_post(post_id)).to_dict()

    async def _download(self, adapter, post, subdir, targets=None, *, overwrite=False):
        await report_progress(f"{adapter.name}：解析作品 {post.id} 的下载目标")
        if targets is None:
            targets = await adapter.get_download_targets(post)
        return (
            await self.downloader.download(post, targets, subdir, overwrite=overwrite)
        ).to_dict()

    def _download_lock(self, site):
        # 不同站点的目录互不重叠；同站点排队，避免同时覆盖同一作品和清单。
        return self._download_locks.setdefault(site, asyncio.Lock())

    async def download_post(
        self, site, post_id, subdir=None, overwrite=False, use_exhentai=None, original=False
    ):
        adapter = self.adapter(site, use_exhentai=use_exhentai, original=original)
        self._validate_id(adapter.name, post_id)
        await report_progress(f"{adapter.name}：等待下载队列")
        async with self._download_lock(adapter.name):
            await report_progress(f"{adapter.name}：获取作品 {post_id} 的详情")
            post = await adapter.get_post(post_id)
            return await self._download(adapter, post, subdir, overwrite=overwrite)

    async def download_url(
        self, url, subdir=None, overwrite=False, use_exhentai=None, original=False
    ):
        for adapter in self.adapters.values():
            if post_id := adapter.parse_url(url.strip()):
                if adapter.name == "ehentai" and use_exhentai is None:
                    use_exhentai = urlsplit(url.strip()).hostname == "exhentai.org"
                return await self.download_post(
                    adapter.name,
                    post_id,
                    subdir,
                    overwrite,
                    use_exhentai=use_exhentai,
                    original=original,
                )
        raise ValidationError("无法识别的 URL", "请提供四个受支持站点的作品或画廊页面链接")

    async def download_search(
        self,
        site,
        query,
        limit=10,
        min_score=None,
        rating=None,
        subdir=None,
        timeout=None,
        overwrite=False,
        use_exhentai=None,
        original=False,
    ):
        adapter = self.adapter(site, use_exhentai=use_exhentai, original=original)
        options = self._search_options(adapter.name, limit, 1, min_score, rating)
        if limit > MAX_BATCH_FILES:
            raise ValidationError("批量下载 limit 最大为 50")
        results, errors, skipped = [], [], []
        attempted = total = reused = 0
        current_id = None
        stop_reason = None
        try:
            async with asyncio.timeout(timeout):
                await report_progress(f"{adapter.name}：等待下载队列")
                async with self._download_lock(adapter.name):
                    await report_progress(f"{adapter.name}：搜索待下载作品")
                    posts = await adapter.search(query, **options)
                    for post in posts:
                        if stop_reason:
                            skipped.append(
                                {"id": post.id, "reason": "站点拒绝访问，已停止本批后续请求"}
                            )
                            continue
                        current_id = post.id
                        remaining = MAX_BATCH_FILES - attempted
                        if post.page_count > remaining or remaining == 0:
                            skipped.append(
                                {
                                    "id": post.id,
                                    "reason": "整本文件数超过本批剩余额度，请单独调用 download_post",
                                }
                            )
                            continue
                        try:
                            targets = await adapter.get_download_targets(post)
                            if len(targets) > remaining:
                                skipped.append(
                                    {
                                        "id": post.id,
                                        "reason": "整本文件数超过本批剩余额度，请单独调用 download_post",
                                    }
                                )
                                continue
                            attempted += len(targets)
                            data = await self._download(
                                adapter, post, subdir, targets, overwrite=overwrite
                            )
                            total += len(data["files"])
                            reused += data.get("reused_files", 0)
                            results.append({"id": post.id, **data})
                            errors.extend(
                                {"id": post.id, **error} for error in data.get("errors", [])
                            )
                            stop_reason = next(
                                (
                                    error
                                    for error in data.get("errors", [])
                                    if error.get("type") in {"auth", "quota"}
                                ),
                                None,
                            )
                        except Exception as exc:
                            error = self._error(exc)
                            errors.append({"id": post.id, **error})
                            if error["type"] in {"auth", "quota"}:
                                stop_reason = error
        except TimeoutError:
            errors.append(
                {
                    "id": current_id,
                    "type": "timeout",
                    "message": "时间预算已耗尽；当前作品的已完成文件记录在其 sidecar 中",
                }
            )
        return {
            "downloaded": results,
            "errors": errors,
            "skipped": skipped,
            "total_files": total,
            "attempted_files": attempted,
            "reused_files": reused,
            "new_files": total - reused,
            "stop_reason": stop_reason,
            "complete": not errors and not skipped,
        }

    async def self_check(self, use_exhentai=None):
        adapters = dict(self.adapters)
        if "ehentai" in adapters:
            adapters["ehentai"] = self.adapter("ehentai", use_exhentai=use_exhentai)

        async def check(name, adapter):
            try:
                async with asyncio.timeout(45):
                    return name, await adapter.check()
            except TimeoutError:
                return name, {"ok": False, "detail": "连通性检查超过 45 秒"}
            except Exception as exc:
                return name, {"ok": False, "detail": self._error(exc)["message"]}

        report = dict(await asyncio.gather(*(check(name, a) for name, a in adapters.items())))
        report["download_root"] = str(self.config.download_root)
        proxy = urlsplit(self.config.network.proxy)
        report["proxy"] = (
            f"{proxy.scheme}://{proxy.hostname}:{proxy.port}" if proxy.hostname else "(直连)"
        )
        return report
