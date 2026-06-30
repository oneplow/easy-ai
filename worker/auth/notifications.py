"""
Dynamic user notifications derived from the user's api_keys row (token quota,
key expiry, RPM limit).

All functions acquire worker.auth.db._lock and use the shared connection.
"""
import math
import time

from .db import _lock, get_conn


def get_user_notifications(username: str) -> list[dict]:
    """Generate dynamic notifications for the user (quota/expiry/rpm warnings)."""
    notifications: list[dict] = []
    now = time.time()

    with _lock:
        c = get_conn()
        try:
            row = c.execute("SELECT * FROM api_keys WHERE owner_username=?", (username,)).fetchone()
            if not row:
                return []

            key = row["key"]

            # --- Token quota ---
            token_limit = row["token_limit"]
            tokens_used = row["tokens_used"] or 0

            if token_limit and token_limit > 0:
                if tokens_used >= token_limit:
                    notifications.append({
                        "id": "token-exceeded",
                        "title": "Token Quota Exceeded",
                        "message": f"You have reached your limit of {token_limit:,} tokens.",
                        "type": "error",
                        "date": int(now * 1000),
                    })
                elif tokens_used >= token_limit * 0.8:
                    notifications.append({
                        "id": "token-low",
                        "title": "Low Token Balance",
                        "message": f"You have used {tokens_used:,} of {token_limit:,} tokens ({(tokens_used / token_limit) * 100:.1f}%).",
                        "type": "warning",
                        "date": int(now * 1000),
                    })

            # --- Key expiration ---
            expires_at = row["expires_at"]
            if expires_at:
                days_left = (expires_at - now) / 86400
                if days_left < 0:
                    notifications.append({
                        "id": "key-expired",
                        "title": "API Key Expired",
                        "message": "Your API key has expired and can no longer be used.",
                        "type": "error",
                        "date": int(now * 1000),
                    })
                elif days_left <= 3:
                    notifications.append({
                        "id": "key-expiring",
                        "title": "API Key Expiring Soon",
                        "message": f"Your API key will expire in {int(days_left)} days.",
                        "type": "warning",
                        "date": int(now * 1000),
                    })

            # --- Current-minute RPM ---
            rpm_limit = row["rpm_limit"]
            if rpm_limit is not None:
                current_minute = math.floor(now / 60)
                limit_row = c.execute(
                    "SELECT count FROM rate_limits WHERE key=? AND minute_timestamp=?",
                    (key, current_minute),
                ).fetchone()

                count = limit_row["count"] if limit_row else 0
                if count >= rpm_limit:
                    notifications.append({
                        "id": "rpm-limit",
                        "title": "Rate Limit Reached",
                        "message": f"You have hit your {rpm_limit} RPM limit. Requests are being throttled.",
                        "type": "error",
                        "date": int(now * 1000),
                    })

            return notifications
        except Exception:
            return []
