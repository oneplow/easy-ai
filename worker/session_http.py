"""
Headless account factory. use.ai signup is two unauthenticated POSTs and needs
NO password, NO email verification (fake emails are accepted; emailVerified stays
null). One free message per account, unlimited accounts per IP -> no proxies.

create_account() -> {email, user_id, cookie_header, token, ua}

Anti-detection: every signup uses a unique fingerprint (randomised UA, headers,
and realistic email). The fingerprint is attached to the returned account dict
so downstream WS connections can reuse the same identity.
"""
import asyncio
import random
import uuid
import logging

import httpx

from . import config
from .fingerprint import fingerprint as make_fingerprint

log = logging.getLogger("session_http")


async def create_account(proxy: str | None = None) -> dict:
    """Sign up a throwaway account. Returns email, user id, cookie header, token, ua."""
    fp = make_fingerprint()
    email = fp["email"]
    hdrs = {**fp["headers"], "Content-Type": "application/json"}

    # Small random delay (50-500ms) to look human
    await asyncio.sleep(random.uniform(0.05, 0.5))

    async with httpx.AsyncClient(timeout=30, headers=hdrs, proxy=proxy) as c:
        r1 = await c.post(f"{config.AUTH_BASE}/email-login", json={"email": email})
        r1.raise_for_status()

        # Small delay between requests like a real browser
        await asyncio.sleep(random.uniform(0.1, 0.4))

        r2 = await c.post(f"{config.AUTH_BASE}/sign-in/credentials", json={
            "email": email,
            "mixpanelUserId": str(uuid.uuid4()),
            "guestId": str(uuid.uuid4()),
            "mid": str(uuid.uuid4()),
        })
        r2.raise_for_status()
        token = r2.headers.get("set-auth-token", "")

        await asyncio.sleep(random.uniform(0.05, 0.2))

        s = await c.get(f"{config.AUTH_BASE}/get-session")
        if s.status_code != 200 or s.text in ("", "null"):
            raise RuntimeError(f"get-session empty after signup ({s.status_code})")
        j = s.json()
        user_id = j["user"]["id"]
        cookie_header = "; ".join(f"{k}={v}" for k, v in c.cookies.items())

    log.info("created account %s (userId=%s)", email, user_id[:8])
    return {"email": email, "user_id": user_id,
            "cookie_header": cookie_header, "token": token,
            "ua": fp["ua"], "headers": fp["headers"]}
