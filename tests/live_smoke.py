"""手动联网检查：safe/Non-H 下载小样；rule34 仅搜索、详情和 CDN HEAD。"""

import asyncio
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from media_mcp.config import load_config
from media_mcp.models import Post
from media_mcp.service import MediaService


async def check_site(service, site, query, rating):
    result = await service.execute(
        "search", site=site, query=query, rating=rating, limit=20, timeout=45
    )
    row = {"search_ok": result["success"], "count": result.get("data", {}).get("count", 0)}
    if not result["success"]:
        row["error"] = result["error"]
        return row
    posts = result["data"]["posts"]
    if not posts:
        row["download_verified"] = False
        row["note"] = "本次查询无匹配作品"
        return row
    try:
        async with asyncio.timeout(90):
            adapter = service.adapters[site]
            post = await adapter.get_post(min(posts, key=lambda p: p["page_count"])["id"])
            row["get_post_ok"] = True
            if site == "rule34":
                target = (await adapter.get_download_targets(post))[0]
                response = await service.network.request(
                    site, "HEAD", target.url, headers={"Referer": post.url}, allow_mirror=False
                )
                row.update(
                    cdn_status=response.status_code,
                    content_type=response.headers.get("content-type"),
                    download_verified=False,
                    note="只检查文件服务器响应，不下载该站媒体小样",
                )
                return row
            targets = (await adapter.get_download_targets(post))[:1]
            sample = Post.from_dict(post.to_dict())
            sample.extra["validation_sample"] = True
            download = await service.downloader.download(sample, targets, "smoke-test")
            row.update(
                download_verified=not download.errors,
                files=len(download.files),
                bytes=sum(f.size for f in download.files),
            )
            if download.errors:
                row["download_errors"] = download.errors
    except Exception as exc:
        row["download_verified"] = False
        row["error"] = service._error(exc)
    return row


async def main():
    root = Path(__file__).resolve().parents[1] / ".validation"
    root.mkdir(exist_ok=True)
    service = MediaService(replace(load_config(), download_root=root / "downloads"))
    report = {"at": datetime.now(UTC).isoformat(), "sites": {}}
    try:
        for site, query, rating in [
            ("e621", "landscape", "safe"),
            ("rule34", "landscape", None),
            ("ehentai", "landscape", "Non-H"),
            ("pixiv", "風景", "safe"),
        ]:
            row = await check_site(service, site, query, rating)
            report["sites"][site] = row
            print(json.dumps({site: row}, ensure_ascii=False), flush=True)
            (root / "live-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    finally:
        await service.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
