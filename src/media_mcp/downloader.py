"""下载引擎：流式临时文件、完整性检查、逐文件结果和可取消的动图合成。"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .config import Config
from .models import (
    AuthError,
    DownloadedFile,
    DownloadResult,
    DownloadTarget,
    MediaMcpError,
    NotFoundError,
    Post,
    QuotaError,
    SiteNetworkError,
    ValidationError,
)
from .network import Network, RateLimiter, check_response

WINDOWS_RESERVED = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DEVICE_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.IGNORECASE)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def sanitize(name: str, max_len: int = 60) -> str:
    cleaned = WINDOWS_RESERVED.sub("_", str(name)).strip(" .")
    cleaned = cleaned[:max_len].strip(" .") or "untitled"
    return "_" + cleaned[1:] if DEVICE_NAME.match(cleaned) else cleaned


def safe_filename(name: str) -> str:
    suffix = Path(name).suffix
    if len(suffix) > 10:
        suffix = ""
    return (
        sanitize(name[: -len(suffix)], 100) + "." + sanitize(suffix[1:], 9)
        if suffix
        else sanitize(name, 110)
    )


def atomic_json(path: Path, payload: dict):
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".part", delete=False
    ) as fh:
        temporary = Path(fh.name)
        try:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        except BaseException:
            fh.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def find_ffmpeg(config: Config):
    configured = config.site_get("pixiv", "ffmpeg_path", "")
    if configured:
        return str(Path(configured).expanduser())
    if executable := shutil.which("ffmpeg"):
        return executable
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return None


class Downloader:
    def __init__(self, config: Config, network: Network):
        self._config = config
        self._network = network
        self._delay_limiters: dict[str, RateLimiter] = {}

    def directory_for(self, post: Post, subdir: str | None = None) -> Path:
        root = Path(self._config.download_root).resolve() / sanitize(post.site)
        if subdir:
            root /= sanitize(subdir, 40)
        if post.site == "ehentai":
            gid = sanitize(post.extra.get("gid") or post.id.split("/")[0], 20)
            root /= f"{gid}_{sanitize(post.title, 40)}"
        elif post.site == "pixiv":
            author = sanitize(post.artist[0], 40) if post.artist else "unknown"
            root /= f"{author}_{sanitize(post.extra.get('user_id', 0), 20)}"
        elif not subdir:
            root /= datetime.now().strftime("%Y-%m-%d")
        if not root.resolve().is_relative_to(Path(self._config.download_root).resolve()):
            raise ValidationError("下载目录超出 download_root")
        return root

    async def download(
        self, post: Post, targets: list[DownloadTarget], subdir=None
    ) -> DownloadResult:
        if not targets:
            raise NotFoundError(f"{post.site} 作品无可下载文件")
        names = [safe_filename(t.filename) for t in targets]
        if len({n.casefold() for n in names}) != len(names):
            raise ValidationError("下载文件名冲突，已停止以防止覆盖")
        directory = self.directory_for(post, subdir)
        directory.mkdir(parents=True, exist_ok=True)
        semaphore = asyncio.Semaphore(
            int(self._config.site_get(post.site, "download_concurrency", 4))
        )
        limiter = self._delay_limiters.setdefault(
            post.site, RateLimiter(float(self._config.site_get(post.site, "download_delay", 0)))
        )
        completed: dict[int, DownloadedFile] = {}
        errors: list[dict] = []
        stopped = asyncio.Event()
        reserved = {name.casefold(): i for i, name in enumerate(names)}

        async def worker(index, target):
            path = directory / names[index]
            try:
                async with semaphore:
                    if stopped.is_set():
                        raise QuotaError("站点已拒绝访问或达到配额，本文件未请求")
                    await limiter.acquire()
                    if stopped.is_set():
                        raise QuotaError("站点已拒绝访问或达到配额，本文件未请求")
                    if target.resolve:
                        target = await target.resolve(target)
                        name = safe_filename(target.filename)
                        if name.casefold() in reserved and reserved[name.casefold()] != index:
                            raise ValidationError("解析后的文件名冲突")
                        reserved[name.casefold()] = index
                        names[index] = name
                        path = directory / name
                    await self._download_one(post.site, target, path)
                    if (
                        target.post_process == "ugoira"
                        and self._config.site_get("pixiv", "ugoira_format", "gif") != "zip"
                    ):
                        path = await self._compose_ugoira(path, target.post_process_meta)
                completed[index] = DownloadedFile(str(path), target.page, path.stat().st_size)
            except Exception as exc:
                if isinstance(exc, (AuthError, QuotaError)):
                    stopped.set()
                detail = (
                    exc.to_dict()
                    if isinstance(exc, MediaMcpError)
                    else {
                        "type": "filesystem" if isinstance(exc, OSError) else "internal",
                        "message": str(exc),
                    }
                )
                errors.append({"page": target.page, "filename": names[index], **detail})

        tasks = [asyncio.create_task(worker(i, t)) for i, t in enumerate(targets)]
        cancelled = False
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            cancelled = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            files = [completed[i] for i in sorted(completed)]
            sidecar = self._write_sidecar(post, directory, files, errors, cancelled, targets)
        return DownloadResult(post, str(directory), files, str(sidecar), errors)

    async def _download_one(self, site: str, target: DownloadTarget, path: Path):
        if path.is_symlink():
            raise ValidationError("目标文件是符号链接")
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as fh:
            temporary = Path(fh.name)
        try:
            for attempt in range(self._config.network.retries):
                try:
                    async with self._network.stream(
                        site, "GET", target.url, headers=target.headers, allow_mirror=False
                    ) as response:
                        check_response(response, site)
                        content_type = response.headers.get("content-type", "").lower()
                        if any(t in content_type for t in ("text/", "json", "xml")):
                            raise SiteNetworkError("下载地址返回网页或错误信息，未保存为媒体文件")
                        total = 0
                        with temporary.open("wb") as output:
                            async for chunk in response.aiter_bytes(128 * 1024):
                                if total == 0 and chunk.lstrip()[:20].lower().startswith(
                                    (b"<!doctype html", b"<html")
                                ):
                                    raise SiteNetworkError("下载地址返回 HTML 验证页面")
                                output.write(chunk)
                                total += len(chunk)
                        expected = response.headers.get("content-length")
                        if total == 0 or (
                            expected
                            and not response.headers.get("content-encoding")
                            and total != int(expected)
                        ):
                            raise httpx.ReadError("文件内容不完整")
                    temporary.replace(path)
                    return
                except httpx.TransportError:
                    if attempt + 1 == self._config.network.retries:
                        raise SiteNetworkError("文件传输中断，已清理未完成文件") from None
                    await asyncio.sleep(min(2**attempt, 8))
        finally:
            temporary.unlink(missing_ok=True)

    def _write_sidecar(self, post, directory, files, errors=None, cancelled=False, targets=()):
        name = "gallery.json" if post.site == "ehentai" else f"{sanitize(post.id, 80)}.json"
        path = directory / name
        atomic_json(
            path,
            {
                "post": post.to_dict(),
                "downloaded_at": datetime.now(UTC).isoformat(),
                "status": "cancelled" if cancelled else "partial" if errors else "complete",
                "files": [{"path": f.path, "page": f.page, "size": f.size} for f in files],
                "errors": errors or [],
                "post_process": [
                    {
                        "filename": t.filename,
                        "type": t.post_process,
                        "metadata": t.post_process_meta,
                    }
                    for t in targets
                    if t.post_process
                ],
            },
        )
        return path

    async def _compose_ugoira(self, zip_path: Path, meta: dict) -> Path:
        executable = find_ffmpeg(self._config)
        if not executable:
            raise SiteNetworkError(
                "未找到 ffmpeg；原始 ZIP 已保留",
                "运行 uv sync --extra animation，或设置 ugoira_format = 'zip'",
            )
        fmt = str(self._config.site_get("pixiv", "ugoira_format", "gif"))
        out = zip_path.with_suffix(f".{fmt}")
        with tempfile.TemporaryDirectory(prefix="ugoira-", dir=zip_path.parent) as folder:
            work = Path(folder)
            with zipfile.ZipFile(zip_path) as archive:
                members = archive.infolist()
                if sum(m.file_size for m in members) > 2 * 1024**3:
                    raise ValidationError("ugoira 解压大小超过 2 GiB")
                for member in members:
                    if Path(member.filename).name != member.filename or not (
                        work / member.filename
                    ).resolve().is_relative_to(work.resolve()):
                        raise ValidationError("ugoira ZIP 包含不安全路径")
                for member in members:
                    with (
                        archive.open(member) as source,
                        (work / member.filename).open("wb") as output,
                    ):
                        while chunk := source.read(128 * 1024):
                            output.write(chunk)
                            await asyncio.sleep(0)
            supplied = meta.get("frames", [])
            frames = (
                [work / f["file"] for f in supplied]
                if supplied
                else sorted(p for p in work.iterdir() if p.suffix.lower() in IMAGE_EXTS)
            )
            if not frames or any(not p.is_file() or p.parent != work for p in frames):
                raise SiteNetworkError("ugoira 帧列表不完整；原始 ZIP 已保留")
            delays = [f["delay"] for f in supplied] if supplied else meta.get("delays", [])
            lines = []
            for i, frame in enumerate(frames):
                # 使用生成的文件名，避免 ffconcat 的路径转义与用户目录单引号问题。
                renamed = work / f"frame_{i:06d}{frame.suffix}"
                shutil.copyfile(frame, renamed)
                lines += [
                    f"file '{renamed.name}'",
                    f"duration {max(1, int(delays[i] if i < len(delays) else 100)) / 1000:.6f}",
                ]
            lines.append(f"file '{renamed.name}'")
            (work / "concat.txt").write_text("\n".join(lines), encoding="utf-8")
            temp_out = work / f"output.{fmt}"
            cmd = [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                "concat.txt",
            ]
            if fmt == "gif":
                cmd += ["-vf", "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse", "-loop", "0"]
            else:
                cmd += [
                    "-vf",
                    "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    "-fps_mode",
                    "vfr",
                ]
            cmd.append(str(temp_out))
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=work, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            try:
                _, stderr = await proc.communicate()
            except BaseException:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
                raise
            if proc.returncode or not temp_out.is_file():
                raise SiteNetworkError(
                    "ffmpeg 合成失败；原始 ZIP 已保留",
                    (stderr or b"").decode(errors="replace")[-300:],
                )
            temp_out.replace(out)
        zip_path.unlink()
        return out
