"""
Headless account factory. use.ai signup is two unauthenticated POSTs and needs
NO password, NO email verification (fake emails are accepted; emailVerified stays
null). One free message per account, unlimited accounts per IP -> no proxies.

create_account() -> {email, user_id, cookie_header, token}
"""
import uuid
import logging

import httpx

from . import config
from .email_gen import gen_email

log = logging.getLogger("session_http")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36")
_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://use.ai",
    "Referer": "https://use.ai/",
    "User-Agent": _UA,
}


async def create_account(proxy: str | None = None) -> dict:
    """Sign up a throwaway account. Returns email, user id, cookie header, token."""
    email = gen_email()
    async with httpx.AsyncClient(timeout=30, headers=_HEADERS, proxy=proxy) as c:
        r1 = await c.post(f"{config.AUTH_BASE}/email-login", json={"email": email})
        r1.raise_for_status()
        r2 = await c.post(f"{config.AUTH_BASE}/sign-in/credentials", json={
            "email": email,
            "mixpanelUserId": str(uuid.uuid4()),
            "guestId": str(uuid.uuid4()),
            "mid": str(uuid.uuid4()),
        })
        r2.raise_for_status()
        token = r2.headers.get("set-auth-token", "")

        s = await c.get(f"{config.AUTH_BASE}/get-session")
        if s.status_code != 200 or s.text in ("", "null"):
            raise RuntimeError(f"get-session empty after signup ({s.status_code})")
        j = s.json()
        user_id = j["user"]["id"]
        cookie_header = "; ".join(f"{k}={v}" for k, v in c.cookies.items())

    log.info("created account %s (userId=%s)", email, user_id[:8])
    return {"email": email, "user_id": user_id,
            "cookie_header": cookie_header, "token": token}
