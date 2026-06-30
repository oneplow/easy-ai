"""
App startup / shutdown lifecycle.

Pulled out of main.py so the entrypoint stays tiny. Owns:
  - the warm account pool (headless WS path) OR the browser prewarmer (fallback)
  - KI store initialization
  - the periodic auth_attempts sweep (multi-worker rate-limiter cleanup)
  - context-store TTL cleanup (see backend/context.py)
"""
import asyncio
import logging

from worker import bank, config
from worker.auth import sweep_auth_attempts
from worker.harvester import top_up

from . import context, knowledge_store

log = logging.getLogger("lifecycle")

_background_tasks: list[asyncio.Task] = []


async def on_startup() -> None:
    """Called from the FastAPI startup event. Idempotent across reloads."""
    if getattr(config, "DIRECT_WS_ENABLED", False):
        from worker.account_pool import POOL
        POOL.start()
        log.info("DIRECT_WS_ENABLED -> headless path, warm account pool started")
    else:
        async def prewarm_loop():
            while True:
                try:
                    n = await top_up()
                    if n:
                        log.info("bank +%d (fresh=%d)", n, bank.count_fresh())
                except Exception as e:
                    log.warning("prewarm error: %s", e)
                await asyncio.sleep(config.PREWARM_INTERVAL_SEC)
        _background_tasks.append(asyncio.create_task(prewarm_loop()))

    # Initialize KI store
    try:
        knowledge_store._get_conn()
        log.info("Knowledge Items store initialized")
    except Exception as e:
        log.warning("KI store init failed (non-fatal): %s", e)

    # Initialize context store (creates the DB / runs migrations)
    try:
        context.init_store()
        log.info("Context store initialized")
    except Exception as e:
        log.warning("Context store init failed (non-fatal): %s", e)

    # Periodic housekeeping: sweep expired auth-attempt rows + context TTL.
    async def housekeeping_loop():
        while True:
            try:
                sweep_auth_attempts()
            except Exception as e:
                log.debug("auth_attempts sweep failed: %s", e)
            try:
                context.sweep_expired()
            except Exception as e:
                log.debug("context sweep failed: %s", e)
            await asyncio.sleep(600)  # every 10 minutes
    _background_tasks.append(asyncio.create_task(housekeeping_loop()))

    # Periodic proxy auto-refresh
    async def proxy_refresh_loop():
        # wait a bit before starting to not block initial startup
        await asyncio.sleep(10)
        from worker import proxy_sources, proxies
        while True:
            try:
                log.info("Auto-refreshing proxy list from sources...")
                await proxy_sources.refresh(limit=1000, concurrency=200)
                proxies.reload()
            except Exception as e:
                log.warning("proxy auto-refresh failed: %s", e)
            await asyncio.sleep(1800)  # every 30 minutes
    _background_tasks.append(asyncio.create_task(proxy_refresh_loop()))


async def on_shutdown() -> None:
    """Cancel background tasks on app shutdown."""
    for t in _background_tasks:
        if not t.done():
            t.cancel()
    _background_tasks.clear()
