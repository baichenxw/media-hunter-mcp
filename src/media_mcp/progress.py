"""按调用隔离的进度通知，不将 MCP 上下文耦合到站点和下载器。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar

ProgressCallback = Callable[[float, float | None, str | None], Awaitable[None]]
logger = logging.getLogger(__name__)


class ProgressReporter:
    def __init__(self, callback: ProgressCallback):
        self.callback = callback
        self.lock = asyncio.Lock()
        self.count = 0
        self.last_sent = 0.0

    async def emit(self, message: str, force: bool):
        async with self.lock:
            if not force and time.monotonic() - self.last_sent < 0.2:
                return
            self.count += 1
            self.last_sent = time.monotonic()
            try:
                # 总工作量含搜索、解析和合成，保持事件计数递增，不伪造百分比。
                await self.callback(self.count, None, message)
            except Exception:
                logger.warning("发送进度通知失败")


_reporter: ContextVar[ProgressReporter | None] = ContextVar("media_progress", default=None)


@contextmanager
def progress_scope(callback: ProgressCallback | None):
    token = _reporter.set(ProgressReporter(callback) if callback else None)
    try:
        yield
    finally:
        _reporter.reset(token)


async def report_progress(message: str, *, force: bool = True):
    if reporter := _reporter.get():
        await reporter.emit(message, force)
