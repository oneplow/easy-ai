"""
Auth database schema, connection management, and shared constants.

This is the foundation module for the worker.auth package. Every other module
(users, api_keys, quotas, usage, notifications) calls get_conn() to obtain a
thread-locked connection.

Connection strategy:
  - One module-level connection, re-used across calls (opened lazily on first
    access and kept alive for the process lifetime).
  - check_same_thread=False because FastAPI serves requests from a thread pool.
  - A single re-entrant lock (_lock) serializes every read/write so concurrent
    threads never corrupt SQLite state or step on each other's transactions.
  - WAL journal mode so multiple worker processes can read while one writes
    (needed once the rate limiter moved off in-memory deques).
"""
import os
import sqlite3
import threading

from .. import config

SESSION_TTL = 3 * 24 * 3600  # 3 days in seconds

# Single serializer for the whole auth DB. Every public function in the
# worker.auth package must acquire this lock around its DB work.
_lock = threading.RLock()

_conn: sqlite3.Connection | None = None


def _init_schema(c: sqlite3.Connection) -> None:
    """Create all auth tables + migrate legacy schemas via additive ALTERs."""
    c.execute("""
        CREATE TABLE IF NOT EXISTS users(
            username TEXT PRIMARY KEY,
            email TEXT,
            password_hash TEXT,
            session_token TEXT,
            session_expires_at REAL,
            created_at REAL
        )""")
    c.execute("""
        CREATE TABLE IF NOT EXISTS api_keys(
            key TEXT PRIMARY KEY,
            name TEXT,
            expires_at REAL,
            rpm_limit INTEGER,
            created_at REAL,
            owner_username TEXT,
            allowed_models TEXT
        )""")

    # --- Additive migrations (idempotent; ignored if the column exists) ---
    for col, ddl in (
        ("email", "ALTER TABLE users ADD COLUMN email TEXT"),
        ("owner_username", "ALTER TABLE api_keys ADD COLUMN owner_username TEXT"),
        ("allowed_models", "ALTER TABLE api_keys ADD COLUMN allowed_models TEXT"),
        ("token_limit", "ALTER TABLE api_keys ADD COLUMN token_limit INTEGER"),
        ("tokens_used", "ALTER TABLE api_keys ADD COLUMN tokens_used INTEGER DEFAULT 0"),
        ("token_reset_period", "ALTER TABLE api_keys ADD COLUMN token_reset_period TEXT DEFAULT 'weekly'"),
        ("token_last_reset", "ALTER TABLE api_keys ADD COLUMN token_last_reset REAL"),
        ("role", "ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'"),
        ("session_expires_at", "ALTER TABLE users ADD COLUMN session_expires_at REAL"),
    ):
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits(
            key TEXT,
            minute_timestamp INTEGER,
            count INTEGER,
            PRIMARY KEY (key, minute_timestamp)
        )""")

    c.execute('''
        CREATE TABLE IF NOT EXISTS request_logs (
                id TEXT PRIMARY KEY,
                username TEXT,
                model TEXT,
                method TEXT,
                url TEXT,
                is_success INTEGER,
                input_tokens INTEGER,
                output_tokens INTEGER,
                latency_ms INTEGER,
                created_at REAL
            )
        ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS usage_logs(
            date TEXT,
            username TEXT,
            model TEXT,
            requests INTEGER DEFAULT 0,
            tokens INTEGER DEFAULT 0,
            success INTEGER DEFAULT 0,
            total_latency_ms INTEGER DEFAULT 0,
            PRIMARY KEY (date, username, model)
        )''')

    # Per-IP auth rate limiter table (moved here from in-memory deques so it
    # survives restarts and is shared across worker processes).
    c.execute("""
        CREATE TABLE IF NOT EXISTS auth_attempts(
            bucket TEXT NOT NULL,
            ip TEXT NOT NULL,
            ts REAL NOT NULL
        )""")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_attempts_lookup "
        "ON auth_attempts(bucket, ip, ts)"
    )

    c.commit()


def _open_fresh() -> sqlite3.Connection:
    """Open a brand-new connection (used by the first get_conn() call)."""
    os.makedirs(os.path.dirname(config.AUTH_DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(config.AUTH_DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    # WAL lets multiple worker processes read while one writes.
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
    except sqlite3.OperationalError:
        pass
    _init_schema(c)
    return c


def get_conn() -> sqlite3.Connection:
    """Return the shared, lazily-initialized connection.

    Callers must hold the module-level _lock while using the returned
    connection — the connection itself is NOT thread-safe on its own.
    """
    global _conn
    if _conn is None:
        _conn = _open_fresh()
    return _conn


def close_conn() -> None:
    """Close the shared connection (mainly useful for tests)."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


# ---------------------------------------------------------------------------
# Per-IP auth rate limiting (shared across workers via the auth_attempts table)
# ---------------------------------------------------------------------------

def check_auth_rate_limit(bucket: str, ip: str, window_start: float, now: float) -> bool:
    """Record an auth attempt for (bucket, ip) and decide if it's allowed.

    Returns True if the attempt is within the allowed quota, False if the
    caller is over the limit (the caller should then 429). One INSERT happens
    per call regardless of the verdict, so the caller is always counted.

    Old rows are opportunistically pruned on each call; a periodic background
    sweep also runs from the app startup (see backend/lifecycle.py) so a quiet
    bucket still gets cleaned.
    """
    import time as _time

    with _lock:
        c = get_conn()
        try:
            # Count recent attempts in this bucket+ip window.
            count = c.execute(
                "SELECT COUNT(*) FROM auth_attempts WHERE bucket=? AND ip=? AND ts >= ?",
                (bucket, ip, window_start),
            ).fetchone()[0]

            allowed = count < config.AUTH_RATE_LIMIT_MAX_REQUESTS

            # Always record the attempt (even rejected ones, so a flood keeps
            # the bucket pinned at over-limit until it cools off).
            c.execute(
                "INSERT INTO auth_attempts(bucket, ip, ts) VALUES (?, ?, ?)",
                (bucket, ip, now),
            )

            # Opportunistic prune: drop rows older than the window for this
            # bucket. Cheap and keeps the table from growing unbounded.
            c.execute(
                "DELETE FROM auth_attempts WHERE bucket=? AND ts < ?",
                (bucket, window_start),
            )
            c.commit()
            return allowed
        except Exception:
            # On any DB error, fail OPEN (allow the attempt) so a transient
            # SQLite hiccup doesn't lock every user out of auth.
            return True


def sweep_auth_attempts(window_sec: int | None = None) -> int:
    """Background sweep: delete all auth_attempts rows older than the window.

    Returns the number of rows pruned. Called periodically from the app
    startup task so even quiet buckets get cleaned."""
    import time as _time

    window = window_sec if window_sec is not None else config.AUTH_RATE_LIMIT_WINDOW_SEC
    cutoff = _time.time() - window
    with _lock:
        c = get_conn()
        try:
            cur = c.execute("DELETE FROM auth_attempts WHERE ts < ?", (cutoff,))
            c.commit()
            return cur.rowcount or 0
        except Exception:
            return 0
