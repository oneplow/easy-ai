"""
API Key authentication database and rate limiter.
Stores client API keys, names, expiration timestamps, and per-minute rate limits.
"""
import math
import os
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()

def _open() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.AUTH_DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(config.AUTH_DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE IF NOT EXISTS api_keys(
            key TEXT PRIMARY KEY,
            name TEXT,
            expires_at REAL,           -- UNIX timestamp, NULL means never expires
            rpm_limit INTEGER,         -- requests per minute limit, NULL means unlimited
            created_at REAL
        )""")
    c.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits(
            key TEXT,
            minute_timestamp INTEGER,
            count INTEGER,
            PRIMARY KEY (key, minute_timestamp)
        )""")
    return c

def create_key(key: str, name: str | None = None, expires_at: float | None = None, rpm_limit: int | None = None) -> dict:
    with _lock:
        c = _open()
        try:
            c.execute(
                "INSERT INTO api_keys(key, name, expires_at, rpm_limit, created_at) VALUES(?,?,?,?,?)",
                (key, name, expires_at, rpm_limit, time.time()),
            )
            c.commit()
            return get_key(key)
        finally:
            c.close()

def get_key(key: str) -> dict | None:
    with _lock:
        c = _open()
        try:
            row = c.execute("SELECT * FROM api_keys WHERE key=?", (key,)).fetchone()
            return dict(row) if row else None
        finally:
            c.close()

def list_keys() -> list[dict]:
    with _lock:
        c = _open()
        try:
            rows = c.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

def delete_key(key: str) -> bool:
    with _lock:
        c = _open()
        try:
            cur = c.execute("DELETE FROM api_keys WHERE key=?", (key,))
            c.execute("DELETE FROM rate_limits WHERE key=?", (key,)) # cleanup history
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()

def validate_and_track_usage(key: str) -> tuple[bool, str]:
    """
    Validates a key against expiration dates and rate limits.
    Returns (True, "") if valid.
    Returns (False, "reason") if invalid, expired, or rate-limited.
    """
    with _lock:
        c = _open()
        try:
            row = c.execute("SELECT * FROM api_keys WHERE key=?", (key,)).fetchone()
            if not row:
                return False, "Invalid API key"
            
            now = time.time()
            if row["expires_at"] and now > row["expires_at"]:
                return False, "API key has expired"
            
            rpm_limit = row["rpm_limit"]
            if rpm_limit is not None:
                current_minute = math.floor(now / 60)
                # Cleanup old limits to prevent DB bloat
                c.execute("DELETE FROM rate_limits WHERE minute_timestamp < ?", (current_minute - 1,))
                
                limit_row = c.execute(
                    "SELECT count FROM rate_limits WHERE key=? AND minute_timestamp=?", 
                    (key, current_minute)
                ).fetchone()
                
                count = limit_row["count"] if limit_row else 0
                if count >= rpm_limit:
                    return False, f"Rate limit exceeded ({rpm_limit} req/min)"
                
                # Increment counter
                c.execute(
                    """
                    INSERT INTO rate_limits(key, minute_timestamp, count) 
                    VALUES(?, ?, 1)
                    ON CONFLICT(key, minute_timestamp) 
                    DO UPDATE SET count=count+1
                    """,
                    (key, current_minute)
                )
                c.commit()
                
            return True, ""
        finally:
            c.close()
