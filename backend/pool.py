"""Concurrency cap. The browser path is throttled hard (each is a full Chromium);
the headless WS path is cheap, so it gets a much higher ceiling."""
import asyncio

from worker import config

_browser_sem = asyncio.Semaphore(config.MAX_CONCURRENT_BROWSERS)
_direct_sem = asyncio.Semaphore(getattr(config, "DIRECT_MAX_CONCURRENCY", 24))


def _sem():
    return _direct_sem if getattr(config, "DIRECT_WS_ENABLED", False) else _browser_sem


async def run_guarded(coro_factory):
    """coro_factory: a zero-arg callable returning the coroutine to run."""
    async with _sem():
        return await coro_factory()


async def run_guarded_gen(gen_factory):
    """Hold the concurrency slot for the whole duration of a streaming generator."""
    async with _sem():
        async for item in gen_factory():
            yield item
