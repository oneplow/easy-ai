"""
API key management: creation, validation, rate-limit tracking, RPM enforcement.

All functions acquire worker.auth.db._lock and use the shared connection.
"""
import math
import secrets
import time

from .. import config
from .db import _lock, get_conn
from .quotas import _auto_reset_if_needed  # validate_and_track_usage uses it


def create_key(key: str, name: str | None = None, expires_at: float | None = None,
               rpm_limit: int | None = None) -> dict:
    """Insert a new API key with the default token quota. Returns the created row."""
    with _lock:
        c = get_conn()
        try:
            now = time.time()
            c.execute(
                "INSERT INTO api_keys(key, name, expires_at, rpm_limit, created_at, token_limit, tokens_used, token_reset_period, token_last_reset) VALUES(?,?,?,?,?,?,?,?,?)",
                (key, name, expires_at, rpm_limit, now, config.DEFAULT_TOKEN_LIMIT, 0, 'weekly', now),
            )
            c.commit()
            return get_key(key)
        except Exception:
            raise


def admin_update_key(key: str, name: str | None = None, rpm_limit: int | None = None,
                     expires_in_days: int | None = None, allowed_models: str | None = None) -> bool:
    with _lock:
        c = get_conn()
        try:
            existing = c.execute(
                "SELECT name, rpm_limit, expires_at, allowed_models FROM api_keys WHERE key=?", (key,)
            ).fetchone()
            if not existing:
                return False

            new_name = name if name is not None else existing["name"]
            new_rpm = rpm_limit if rpm_limit is not None else existing["rpm_limit"]
            new_expires = (time.time() + (expires_in_days * 86400)) if expires_in_days is not None else existing["expires_at"]
            new_models = allowed_models if allowed_models is not None else existing["allowed_models"]

            c.execute("""
                UPDATE api_keys
                SET name=?, rpm_limit=?, expires_at=?, allowed_models=?
                WHERE key=?
            """, (new_name, new_rpm, new_expires, new_models, key))
            c.commit()
            return True
        except Exception:
            return False


def get_key(key: str) -> dict | None:
    with _lock:
        c = get_conn()
        try:
            row = c.execute("SELECT * FROM api_keys WHERE key=?", (key,)).fetchone()
            return dict(row) if row else None
        except Exception:
            return None


def get_username_from_key(key: str) -> str | None:
    with _lock:
        c = get_conn()
        try:
            row = c.execute("SELECT owner_username FROM api_keys WHERE key=?", (key,)).fetchone()
            return row["owner_username"] if row and row["owner_username"] else None
        except Exception:
            return None


def list_keys() -> list[dict]:
    with _lock:
        c = get_conn()
        try:
            rows = c.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


def delete_key(key: str) -> bool:
    with _lock:
        c = get_conn()
        try:
            cur = c.execute("DELETE FROM api_keys WHERE key=?", (key,))
            c.execute("DELETE FROM rate_limits WHERE key=?", (key,))  # cleanup history
            c.commit()
            return cur.rowcount > 0
        except Exception:
            return False


def reset_limit(key: str) -> bool:
    """Instantly reset the per-minute rate-limit counter for a key."""
    with _lock:
        c = get_conn()
        try:
            cur = c.execute("DELETE FROM rate_limits WHERE key=?", (key,))
            c.commit()
            return cur.rowcount > 0
        except Exception:
            return False


def validate_and_track_usage(key: str, model: str) -> tuple[bool, str]:
    """
    Validates a key against allowed models, expiration, token quota, and the
    per-minute RPM limit, incrementing the counter on success.

    Returns (True, "") if valid, else (False, "reason").
    """
    with _lock:
        c = get_conn()
        try:
            row = c.execute("SELECT * FROM api_keys WHERE key=?", (key,)).fetchone()
            if not row:
                return False, "Invalid API key"

            allowed_models = row["allowed_models"]
            if allowed_models:
                models_list = [m.strip().lower() for m in allowed_models.split(",") if m.strip()]
                if models_list and model.lower() not in models_list:
                    return False, f"Model '{model}' is not allowed for this API key"

            now = time.time()
            if row["expires_at"] and now > row["expires_at"]:
                return False, "API key has expired"

            # --- Token quota check (auto-resets on schedule) ---
            token_limit = row["token_limit"]
            if token_limit is not None:
                _auto_reset_if_needed(c, key, row)
                row = c.execute("SELECT * FROM api_keys WHERE key=?", (key,)).fetchone()
                tokens_used = row["tokens_used"] or 0
                if tokens_used >= token_limit:
                    return False, f"Token quota exceeded ({tokens_used}/{token_limit} tokens used)"

            # --- RPM check ---
            rpm_limit = row["rpm_limit"]
            if rpm_limit is not None:
                current_minute = math.floor(now / 60)
                c.execute("DELETE FROM rate_limits WHERE minute_timestamp < ?", (current_minute - 1,))

                limit_row = c.execute(
                    "SELECT count FROM rate_limits WHERE key=? AND minute_timestamp=?",
                    (key, current_minute),
                ).fetchone()

                count = limit_row["count"] if limit_row else 0
                if count >= rpm_limit:
                    return False, f"Rate limit exceeded ({rpm_limit} req/min)"

                c.execute(
                    """
                    INSERT INTO rate_limits(key, minute_timestamp, count)
                    VALUES(?, ?, 1)
                    ON CONFLICT(key, minute_timestamp)
                    DO UPDATE SET count=count+1
                    """,
                    (key, current_minute),
                )
                c.commit()

            return True, ""
        except Exception as e:
            return False, f"Validation error: {e}"


def _auto_create_user_key(c, username: str, now: float) -> None:
    """Auto-create an API key for a new user with the default token limit.
    Must be called within an existing lock+connection."""
    key = "sk-" + secrets.token_hex(32)
    token_limit = config.DEFAULT_TOKEN_LIMIT
    c.execute("""
        INSERT INTO api_keys(key, name, expires_at, rpm_limit, created_at, owner_username, allowed_models, token_limit, tokens_used, token_reset_period, token_last_reset)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, 'weekly', ?)
    """, (key, f"{username}'s Key", None, 60, now, username, None, token_limit, now))


def create_or_update_user_key(username: str, rpm_limit: int, expires_in_days: int | None,
                              allowed_models: str | None,
                              default_token_limit: int | None = 1_000_000) -> dict:
    """Creates or updates the single key for a user.
    default_token_limit: default token quota for new keys (default 1M tokens)."""
    with _lock:
        c = get_conn()
        try:
            if rpm_limit > 60:
                rpm_limit = 60

            now = time.time()
            expires_at = now + (expires_in_days * 86400) if expires_in_days else None

            existing = c.execute("SELECT key FROM api_keys WHERE owner_username=?", (username,)).fetchone()
            if existing:
                key = existing["key"]
                c.execute("""
                    UPDATE api_keys
                    SET rpm_limit=?, expires_at=?, allowed_models=?
                    WHERE key=?
                """, (rpm_limit, expires_at, allowed_models, key))
            else:
                key = "sk-" + secrets.token_hex(32)
                c.execute("""
                    INSERT INTO api_keys(key, name, expires_at, rpm_limit, created_at, owner_username, allowed_models, token_limit, token_last_reset)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (key, f"{username}'s Key", expires_at, rpm_limit, now, username, allowed_models, default_token_limit, now))

            c.commit()
            return {"key": key, "rpm_limit": rpm_limit, "expires_at": expires_at, "allowed_models": allowed_models}
        except Exception:
            raise


def admin_update_user_key(username: str, rpm_limit: int | None = None,
                          expires_in_days: int | None = None,
                          allowed_models: str | None = None) -> bool:
    with _lock:
        c = get_conn()
        try:
            existing = c.execute(
                "SELECT key, rpm_limit, expires_at, allowed_models FROM api_keys WHERE owner_username=?",
                (username,),
            ).fetchone()
            if not existing:
                return False

            new_rpm = rpm_limit if rpm_limit is not None else existing["rpm_limit"]
            new_expires = (time.time() + (expires_in_days * 86400)) if expires_in_days is not None else existing["expires_at"]
            new_models = allowed_models if allowed_models is not None else existing["allowed_models"]

            c.execute("""
                UPDATE api_keys
                SET rpm_limit=?, expires_at=?, allowed_models=?
                WHERE key=?
            """, (new_rpm, new_expires, new_models, existing["key"]))
            c.commit()
            return True
        except Exception:
            return False


def get_user_key(username: str) -> dict | None:
    with _lock:
        c = get_conn()
        try:
            row = c.execute("SELECT * FROM api_keys WHERE owner_username=?", (username,)).fetchone()
            return dict(row) if row else None
        except Exception:
            return None
