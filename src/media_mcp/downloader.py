"""下载引擎：流式临时文件、完整性检查、逐文件结果和可取消的动图合成。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import asdict
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
from .network import RETRYABLE_STATUS, Network, RateLimiter, check_response
from .progress import report_progress

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
        self, post: Post, targets: list[DownloadTarget], subdir=None, *, overwrite=False
    ) -> DownloadResult:
        if not targets:
            raise NotFoundError(f"{post.site} 作品无可下载文件")
        names = [safe_filename(t.filename) for t in targets]
        if len({n.casefold() for n in names}) != len(names):
            raise ValidationError("下载文件名冲突，已停止以防止覆盖")
        directory = self.directory_for(post, subdir)
        directory.mkdir(parents=True, exist_ok=True)
        source_keys = [self._source_key(t) for t in targets]
        previous = self._load_manifest(post, directory)
        previous = {k: v for k, v in previous.items() if k in source_keys}
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
        await report_progress(f"{post.site}：准备处理 {len(targets)} 个文件，校验已有文件")

        def reserve_name(index, name):
            if name.casefold() in reserved and reserved[name.casefold()] != index:
                raise ValidationError("解析后的文件名冲突")
            reserved[name.casefold()] = index
            names[index] = name

        async def worker(index, target):
            path = directory / names[index]
            try:
                async with semaphore:
                    if not overwrite:
                        cached = await self._reuse(previous.get(source_keys[index]), directory)
                        if cached:
                            reserve_name(index, Path(cached.path).name)
                            completed[index] = cached
                            await report_progress(
                                f"{post.site}：已处理 {len(completed) + len(errors)}/{len(targets)}，复用已校验文件",
                                force=False,
                            )
                            return
                    if stopped.is_set():
                        raise QuotaError("站点已拒绝访问或达到配额，本文件未请求")
                    await limiter.acquire()
                    if stopped.is_set():
                        raise QuotaError("站点已拒绝访问或达到配额，本文件未请求")
                    if target.resolve:
                        target = await target.resolve(target)
                        name = safe_filename(target.filename)
                        reserve_name(index, name)
                        path = directory / name
                    digest = await self._download_one(post.site, target, path)
                    if (
                        target.post_process == "ugoira"
                        and self._config.site_get("pixiv", "ugoira_format", "gif") != "zip"
                    ):
                        await report_progress(f"pixiv：合成作品 {post.id} 的动图")
                        path = await self._compose_ugoira(path, target.post_process_meta)
                        digest = None
                    digest = digest or await self._hash_file(path)
                    completed[index] = DownloadedFile(
                        str(path), target.page, path.stat().st_size, digest, source_keys[index]
                    )
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
            await report_progress(
                f"{post.site}：已处理 {len(completed) + len(errors)}/{len(targets)}，失败 {len(errors)}",
                force=False,
            )

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
            sidecar = self._write_sidecar(
                post, directory, files, errors, cancelled, targets, previous
            )
        return DownloadResult(post, str(directory), files, str(sidecar), errors)

    def _source_key(self, target):
        identity = {
            "url": target.url,
            "filename": target.filename,
            "page": target.page,
            "process": target.post_process,
            "metadata": target.post_process_meta,
            "format": self._config.site_get("pixiv", "ugoira_format", "gif")
            if target.post_process
            else None,
            "composition_version": 2 if target.post_process else None,
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    @staticmethod
    async def _hash_file(path):
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                await asyncio.sleep(0)
        return digest.hexdigest()

    @staticmethod
    def _sidecar_path(post, directory):
        name = "gallery.json" if post.site == "ehentai" else f"{sanitize(post.id, 80)}.json"
        return directory / name

    def _load_manifest(self, post, directory):
        try:
            path = self._sidecar_path(post, directory)
            if path.is_symlink():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["post"]["id"] != post.id or data["post"]["site"] != post.site:
                return {}
            return {
                row["source_key"]: row
                for row in data["files"]
                if isinstance(row, dict)
                and isinstance(row.get("source_key"), str)
                and re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256", "")))
                and isinstance(row.get("path"), str)
                and isinstance(row.get("size"), int)
                and isinstance(row.get("page"), int)
                and Path(row["path"]).parent.resolve() == directory.resolve()
            }
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    async def _reuse(self, row, directory):
        if not row:
            return None
        path = Path(row["path"])
        try:
            if (
                path.is_symlink()
                or path.parent.resolve() != directory.resolve()
                or not path.is_file()
                or path.stat().st_size != row["size"]
                or row["size"] <= 0
            ):
                return None
            if await self._hash_file(path) != row["sha256"]:
                return None
            return DownloadedFile(
                str(path), row["page"], row["size"], row["sha256"], row["source_key"], True
            )
        except OSError:
            return None

    async def _download_one(self, site: str, target: DownloadTarget, path: Path):
        if path.is_symlink():
            raise ValidationError("目标文件是符号链接")
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as fh:
            temporary = Path(fh.name)
        try:
            for attempt in range(self._config.network.retries):
                delay = Network._backoff(attempt)
                try:
                    async with self._network.stream(
                        site,
                        "GET",
                        target.url,
                        headers=target.headers,
                        allow_mirror=False,
                        retry=False,
                    ) as response:
                        if (
                            response.status_code in RETRYABLE_STATUS
                            and attempt + 1 < self._config.network.retries
                        ):
                            delay = Network._backoff(attempt, response)
                            # 等待前释放响应和连接；上下文退出时的重复关闭是幂等的。
                            await response.aclose()
                            await asyncio.sleep(delay)
                            continue
                        check_response(response, site)
                        content_type = response.headers.get("content-type", "").lower()
                        if any(t in content_type for t in ("text/", "json", "xml")):
                            raise SiteNetworkError("下载地址返回网页或错误信息，未保存为媒体文件")
                        total = 0
                        digest = hashlib.sha256()
                        with temporary.open("wb") as output:
                            async for chunk in response.aiter_bytes(128 * 1024):
                                if total == 0 and chunk.lstrip()[:20].lower().startswith(
                                    (b"<!doctype html", b"<html")
                                ):
                                    raise SiteNetworkError("下载地址返回 HTML 验证页面")
                                output.write(chunk)
                                digest.update(chunk)
                                total += len(chunk)
                        expected = response.headers.get("content-length")
                        if total == 0 or (
                            expected
                            and not response.headers.get("content-encoding")
                            and total != int(expected)
                        ):
                            raise httpx.ReadError("文件内容不完整")
                    temporary.replace(path)
                    return digest.hexdigest()
                except httpx.TransportError:
                    if attempt + 1 == self._config.network.retries:
                        raise SiteNetworkError("文件传输中断，已清理未完成文件") from None
                    await asyncio.sleep(delay)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_sidecar(
        self, post, directory, files, errors=None, cancelled=False, targets=(), previous=None
    ):
        path = self._sidecar_path(post, directory)
        # 取消或重试失败时保留旧文件索引；下次复用仍须重新校验大小和哈希。
        inventory = dict(previous or {})
        for file in files:
            inventory[file.source_key] = asdict(file)
        atomic_json(
            path,
            {
                "post": post.to_dict(),
                "downloaded_at": datetime.now(UTC).isoformat(),
                "status": "cancelled" if cancelled else "partial" if errors else "complete",
                "files": sorted(inventory.values(), key=lambda row: row["page"]),
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
            extracted = work / "source"
            extracted.mkdir()
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
                        (extracted / member.filename).open("wb") as output,
                    ):
                        while chunk := source.read(128 * 1024):
                            output.write(chunk)
                            await asyncio.sleep(0)
            supplied = meta.get("frames", [])
            frames = (
                [extracted / f["file"] for f in supplied]
                if supplied
                else sorted(p for p in extracted.iterdir() if p.suffix.lower() in IMAGE_EXTS)
            )
            if not frames or any(not p.is_file() or p.parent != extracted for p in frames):
                raise SiteNetworkError("ugoira 帧列表不完整；原始 ZIP 已保留")
            if any(p.suffix.lower() not in IMAGE_EXTS for p in frames):
                raise ValidationError("ugoira 帧必须是受支持的图片文件")
            delays = [f["delay"] for f in supplied] if supplied else meta.get("delays", [])
            durations = [
                max(1, int(delays[i] if i < len(delays) else 100)) for i in range(len(frames))
            ]
            if fmt == "gif":
                # GIF 只能表达百分之一秒；MP4 保留毫秒精度。
                durations = [max(10, (delay + 5) // 10 * 10) for delay in durations]
            lines = []
            for i, frame in enumerate(frames):
                # 使用生成的文件名，避免 ffconcat 的路径转义与用户目录单引号问题。
                renamed = work / f"frame_{i:06d}{frame.suffix}"
                with frame.open("rb") as source, renamed.open("wb") as output:
                    while chunk := source.read(128 * 1024):
                        output.write(chunk)
                        await asyncio.sleep(0)
                duration = durations[i]
                if fmt == "mp4" and i == len(frames) - 1 and duration > 1:
                    duration -= 1
                lines += [
                    f"file '{renamed.name}'",
                    "option framerate 1000",
                    f"duration {duration / 1000:.6f}",
                ]
            # MP4 末帧默认仅长 1 ms；用相同图像在末尾补齐，不延长总时长。
            if fmt == "mp4" and durations[-1] > 1:
                lines += [f"file '{renamed.name}'", "option framerate 1000"]
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
                "0",  # option 指令要求 safe=0；清单仅包含上面生成并验证的本地文件名。
                "-i",
                "concat.txt",
                "-fps_mode",
                "vfr",
                "-enc_time_base",
                "1:1000",
            ]
            if fmt == "gif":
                cmd += [
                    "-vf",
                    "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
                    "-loop",
                    "0",
                    "-final_delay",
                    str(durations[-1] // 10),
                ]
            else:
                cmd += [
                    "-vf",
                    "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                    "-c:v",
                    "libx264",
                    "-bf",
                    "0",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    "-video_track_timescale",
                    "1000",
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
