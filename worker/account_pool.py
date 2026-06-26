"""
Warm account pool. Signup is ~1s of HTTP; keeping a few ready accounts in memory
takes it out of the hot path so a request only pays for the WS stream.

Each account is single-use (1 free message). The background loop tops the pool
back up to ACCOUNT_POOL_SIZE; acquire() hands one out, or signs up inline if the
pool is drained (graceful degradation under burst load).
"""
import asyncio
import logging
import random
import time

from . import config
from . import proxies
from .session_http import create_account

log = logging.getLogger("account_pool")


class AccountPool:
    def __init__(self):
        self.size = getattr(config, "ACCOUNT_POOL_SIZE", 8)
        self.ttl = getattr(config, "ACCOUNT_TTL_SEC", 600)
        self.refill_sec = getattr(config, "ACCOUNT_POOL_REFILL_SEC", 3)
        self._q: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None

    def _queue(self) -> asyncio.Queue:
        if self._q is None:
            self._q = asyncio.Queue(maxsize=self.size)
        return self._q

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            log.info("account pool started (target=%d)", self.size)

    async def _make_one(self) -> None:
        p = proxies.to_url(proxies.next_proxy())
        a = await create_account(proxy=p)
        a["_born"] = time.time()
        a["proxy"] = p
        await self._queue().put(a)

    async def _loop(self) -> None:
        while True:
            try:
                deficit = self.size - self._queue().qsize()
                if deficit > 0:
                    await asyncio.gather(*[self._make_one() for _ in range(deficit)],
                                         return_exceptions=True)
            except Exception as e:
                log.warning("pool loop error: %s", e)
            await asyncio.sleep(self.refill_sec + random.uniform(0.5, 2.0))

    async def acquire(self) -> dict:
        """A warm account if one is ready (and not stale); otherwise sign up inline."""
        q = self._queue()
        while not q.empty():
            try:
                a = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if time.time() - a.get("_born", 0) < self.ttl:
                return a               # fresh enough -> use it
            # stale -> drop and try the next one
        p = proxies.to_url(proxies.next_proxy())
        a = await create_account(proxy=p)  # drained -> ~1s inline signup
        a["proxy"] = p
        return a

    def ready(self) -> int:
        return self._q.qsize() if self._q else 0


POOL = AccountPool()
