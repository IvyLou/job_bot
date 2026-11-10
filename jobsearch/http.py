from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Awaitable, Callable, Iterable, Tuple, TypeVar

import httpx

from . import config

T = TypeVar("T")
R = TypeVar("R")


def web_client() -> httpx.AsyncClient:
    h = config.search()["http"]
    return httpx.AsyncClient(
        headers={"User-Agent": h["web_user_agent"],
                 "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                 "Accept-Language": "en-US,en;q=0.9"},
        timeout=h["timeout_seconds"], follow_redirects=True,
        limits=httpx.Limits(max_connections=h["concurrency"] * 2, max_keepalive_connections=h["concurrency"]))


def sec_client() -> httpx.Client:
    h = config.search()["http"]
    return httpx.Client(headers={"User-Agent": h["sec_user_agent"]}, timeout=30, follow_redirects=True)


class Throttle:
    """Space out synchronous requests, e.g. to stay under the SEC's 10 requests/second limit."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self.next_at = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self.next_at:
            time.sleep(self.next_at - now)
        self.next_at = max(now, self.next_at) + self.interval


async def bounded(items: Iterable[T], worker: Callable[[T], Awaitable[R]], limit: int) -> AsyncIterator[Tuple[T, R]]:
    """Run worker over items with at most `limit` in flight, yielding (item, result) as each finishes."""
    sem = asyncio.Semaphore(limit)

    async def run(item: T) -> Tuple[T, R]:
        async with sem:
            return item, await worker(item)

    for fut in asyncio.as_completed([run(i) for i in items]):
        yield await fut
