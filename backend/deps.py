"""
Shared request dependencies: auth guards, per-IP rate limiting, and the
dashboard payload builder.

Every router imports from here rather than re-implementing header parsing /
session validation. Splitting these out of the old main.py monolith was the
key precondition for splitting the routes into routers — they all share this
cross-cutting layer.

Rate limiting note: `enforce_auth_rate_limit` is backed by the auth_attempts
table (worker.auth.db), so the count is shared across worker processes and
survives restarts. Previously this was an in-process `defaultdict(deque)`,
which a multi-worker deployment (uvicorn --workers N) would silently bypass.
"""
import logging
import time

from fastapi import HTTPException, Request

from worker import auth_db, bank, config, health

log = logging.getLogger("deps")


# ---------------------------------------------------------------------------
# IP extraction
# ---------------------------------------------------------------------------

def get_client_ip(req: Request) -> str:
    """Best-effort client IP. Honors X-Forwarded-For / X-Real-IP when present
    (e.g. behind a reverse proxy), else falls back to the socket peer."""
    forwarded_for = req.headers.get("x-forwarded-for", "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = req.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    return req.client.host if req.client else "unknown"


# ---------------------------------------------------------------------------
# Per-IP auth rate limiting (SQLite-backed, multi-worker safe)
# ---------------------------------------------------------------------------

def enforce_auth_rate_limit(req: Request, bucket: str) -> None:
    """Reject with 429 if the caller's IP has hit too many auth attempts in
    the configured window. Backed by the shared auth_attempts table so the
    limit holds across processes."""
    now = time.time()
    window_start = now - config.AUTH_RATE_LIMIT_WINDOW_SEC
    client_ip = get_client_ip(req)
    is_valid = auth_db.check_auth_rate_limit(bucket, client_ip, window_start, now)
    if not is_valid:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many auth attempts. Try again in "
                f"{config.AUTH_RATE_LIMIT_WINDOW_SEC} seconds."
            ),
        )


# ---------------------------------------------------------------------------
# Auth guards
# ---------------------------------------------------------------------------

def _bearer_token(req: Request) -> str:
    """Extract a Bearer token from the Authorization header, or "" if absent."""
    auth = req.headers.get("authorization", "")
    return auth.removeprefix("Bearer ").strip() if auth.lower().startswith("bearer ") else ""


def require_admin_key(req: Request) -> None:
    """Legacy admin auth: accepts ADMIN_KEY bearer/x-api-key OR a role-based
    admin session token. Raises 401 otherwise."""
    token = _bearer_token(req)

    if token:
        # Role-based admin session first
        user_info = auth_db.get_full_user_by_token(token)
        if user_info and user_info.get("role") == "admin":
            return
        # Then ADMIN_KEY fallback
        if config.ADMIN_KEY and token == config.ADMIN_KEY:
            return

    # x-api-key header fallback
    key = req.headers.get("x-api-key", "").strip()
    if config.ADMIN_KEY and key == config.ADMIN_KEY:
        return

    raise HTTPException(status_code=401, detail="Admin access required")


def require_api_key(req: Request, model: str) -> str:
    """Validate a client API key (Bearer or x-api-key) for the given model,
    tracking RPM/quota usage. Returns the validated key string."""
    token = _bearer_token(req)
    key = req.headers.get("x-api-key", "").strip()

    client_key = token or key
    if not client_key:
        raise HTTPException(status_code=401, detail="Missing API key")

    is_valid, reason = auth_db.validate_and_track_usage(client_key, model)
    if not is_valid:
        raise HTTPException(status_code=401, detail=reason)
    return client_key


def get_user_from_req(req: Request) -> str:
    """Resolve the session bearer token to a username. Raises 401 if missing
    or invalid."""
    token = _bearer_token(req)
    if not token:
        raise HTTPException(status_code=401, detail="Missing session token")
    username = auth_db.get_user_from_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid session token")
    return username


# ---------------------------------------------------------------------------
# Dashboard payload (health stream)
# ---------------------------------------------------------------------------

def get_dashboard_payload(req: Request) -> dict:
    """Build the SSE dashboard snapshot: health + (admin|user) usage summary.

    Same logic as the old main.py `_get_dashboard_payload`, preserved verbatim
    so the frontend dashboard stream keeps its shape."""
    if getattr(config, "DIRECT_WS_ENABLED", False):
        from worker.account_pool import POOL
        snap = health.H.snapshot(POOL.ready())
        snap["warm_accounts"] = POOL.ready()
        snap["pool_target"] = POOL.size
    else:
        snap = health.H.snapshot(bank.count_fresh())

    bearer = _bearer_token(req)
    key = req.headers.get("x-api-key", "").strip()

    dashboard: dict | None = None

    # Role-based admin first, then ADMIN_KEY fallback
    is_admin = False
    if bearer:
        user_info = auth_db.get_full_user_by_token(bearer)
        if user_info and user_info.get("role") == "admin":
            is_admin = True
    if not is_admin and config.ADMIN_KEY and (bearer == config.ADMIN_KEY or key == config.ADMIN_KEY):
        is_admin = True

    if is_admin:
        dashboard = {
            "mode": "admin",
            "key_count": len(auth_db.list_keys()),
            "user_count": len(auth_db.get_all_users()),
            "total_system_tokens": auth_db.get_total_system_tokens(),
            "token_info": None,
        }
    elif bearer:
        username = auth_db.get_user_from_token(bearer)
        if username:
            dashboard = {
                "mode": "user",
                "key_count": 1 if auth_db.get_user_key(username) else 0,
                "user_count": None,
                "token_info": auth_db.get_token_usage_by_username(username)
                or {
                    "token_limit": config.DEFAULT_TOKEN_LIMIT,
                    "tokens_used": 0,
                    "token_reset_period": "weekly",
                    "token_last_reset": None,
                },
            }

    snap["dashboard"] = dashboard
    return snap
