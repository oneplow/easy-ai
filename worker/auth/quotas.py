"""
Token quota management: consume, query, reset, and admin adjustments.

Token auto-reset cadences: daily, weekly, biweekly, monthly, never.
All functions acquire worker.auth.db._lock and use the shared connection.
"""
import time

from .db import _lock, get_conn


def _auto_reset_if_needed(c, key: str, row) -> None:
    """Reset token usage if the configured period has elapsed.
    Called within an existing lock+connection by validate_and_track_usage."""
    reset_period = row["token_reset_period"] or "weekly"
    last_reset = row["token_last_reset"] or row["created_at"] or 0
    now = time.time()

    period_seconds = {
        "daily": 86400,
        "weekly": 7 * 86400,
        "biweekly": 14 * 86400,
        "monthly": 30 * 86400,
        "never": float("inf"),
    }
    interval = period_seconds.get(reset_period, 7 * 86400)

    if now - last_reset >= interval:
        c.execute("UPDATE api_keys SET tokens_used=0, token_last_reset=? WHERE key=?", (now, key))
        c.commit()


def consume_tokens(key: str, count: int) -> bool:
    """Add `count` tokens to the usage for this key. Returns True if successful."""
    if count <= 0:
        return True
    with _lock:
        c = get_conn()
        try:
            c.execute(
                "UPDATE api_keys SET tokens_used = COALESCE(tokens_used, 0) + ? WHERE key=?",
                (count, key),
            )
            c.commit()
            return True
        except Exception:
            return False


def get_token_usage(key: str) -> dict | None:
    """Get token limit and usage for a key."""
    with _lock:
        c = get_conn()
        try:
            row = c.execute(
                "SELECT token_limit, tokens_used, token_reset_period, token_last_reset FROM api_keys WHERE key=?",
                (key,),
            ).fetchone()
            if not row:
                return None
            return {
                "token_limit": row["token_limit"],
                "tokens_used": row["tokens_used"] or 0,
                "token_reset_period": row["token_reset_period"] or "weekly",
                "token_last_reset": row["token_last_reset"],
            }
        except Exception:
            return None


def get_token_usage_by_username(username: str) -> dict | None:
    """Get token usage for a user's key."""
    with _lock:
        c = get_conn()
        try:
            row = c.execute(
                "SELECT token_limit, tokens_used, token_reset_period, token_last_reset, key FROM api_keys WHERE owner_username=?",
                (username,),
            ).fetchone()
            if not row:
                return None
            return {
                "token_limit": row["token_limit"],
                "tokens_used": row["tokens_used"] or 0,
                "token_reset_period": row["token_reset_period"] or "weekly",
                "token_last_reset": row["token_last_reset"],
                "key": row["key"],
            }
        except Exception:
            return None


def get_total_system_tokens() -> int:
    """Sum of all tokens used across all api keys."""
    with _lock:
        c = get_conn()
        try:
            row = c.execute("SELECT SUM(tokens_used) as total FROM api_keys").fetchone()
            return row["total"] or 0
        except Exception:
            return 0


def admin_set_token_limit(key: str, token_limit: int | None,
                          reset_period: str | None = None) -> bool:
    """Set or remove the token limit for a key. None = unlimited."""
    with _lock:
        c = get_conn()
        try:
            existing = c.execute("SELECT 1 FROM api_keys WHERE key=?", (key,)).fetchone()
            if not existing:
                return False
            if reset_period:
                c.execute(
                    "UPDATE api_keys SET token_limit=?, token_reset_period=? WHERE key=?",
                    (token_limit, reset_period, key),
                )
            else:
                c.execute("UPDATE api_keys SET token_limit=? WHERE key=?", (token_limit, key))
            c.commit()
            return True
        except Exception:
            return False


def admin_set_token_limit_by_username(username: str, token_limit: int | None,
                                       reset_period: str | None = None) -> bool:
    """Set token limit for a user's key by username."""
    with _lock:
        c = get_conn()
        try:
            row = c.execute("SELECT key FROM api_keys WHERE owner_username=?", (username,)).fetchone()
            if not row:
                return False
            key = row["key"]
            if reset_period:
                c.execute(
                    "UPDATE api_keys SET token_limit=?, token_reset_period=? WHERE key=?",
                    (token_limit, reset_period, key),
                )
            else:
                c.execute("UPDATE api_keys SET token_limit=? WHERE key=?", (token_limit, key))
            c.commit()
            return True
        except Exception:
            return False


def admin_reset_tokens(key: str) -> bool:
    """Reset tokens_used to 0 for a key."""
    with _lock:
        c = get_conn()
        try:
            c.execute(
                "UPDATE api_keys SET tokens_used=0, token_last_reset=? WHERE key=?",
                (time.time(), key),
            )
            c.commit()
            return True
        except Exception:
            return False


def admin_reset_tokens_by_username(username: str) -> bool:
    """Reset tokens_used for a user's key."""
    with _lock:
        c = get_conn()
        try:
            row = c.execute("SELECT key FROM api_keys WHERE owner_username=?", (username,)).fetchone()
            if not row:
                return False
            c.execute(
                "UPDATE api_keys SET tokens_used=0, token_last_reset=? WHERE key=?",
                (time.time(), row["key"]),
            )
            c.commit()
            return True
        except Exception:
            return False


def admin_add_tokens(key: str, amount: int) -> bool:
    """Increase the token_limit by `amount` for a key."""
    with _lock:
        c = get_conn()
        try:
            row = c.execute("SELECT token_limit FROM api_keys WHERE key=?", (key,)).fetchone()
            if not row:
                return False
            current = row["token_limit"] or 0
            c.execute("UPDATE api_keys SET token_limit=? WHERE key=?", (current + amount, key))
            c.commit()
            return True
        except Exception:
            return False


def admin_add_tokens_by_username(username: str, amount: int) -> bool:
    """Increase token_limit for a user's key by username."""
    with _lock:
        c = get_conn()
        try:
            row = c.execute(
                "SELECT key, token_limit FROM api_keys WHERE owner_username=?", (username,)
            ).fetchone()
            if not row:
                return False
            current = row["token_limit"] or 0
            c.execute(
                "UPDATE api_keys SET token_limit=? WHERE key=?",
                (current + amount, row["key"]),
            )
            c.commit()
            return True
        except Exception:
            return False
