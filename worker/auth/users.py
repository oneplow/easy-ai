"""
User management: registration, login (password + Google), sessions, roles.

All functions acquire worker.auth.db._lock and use the shared connection.
"""
import secrets
import time

import bcrypt

from .. import config
from .db import SESSION_TTL, _lock, get_conn


def register_user(username: str, password: str, email: str | None = None) -> tuple[bool, str, str]:
    """Register a user. Returns (success, token_or_error, role)."""
    with _lock:
        c = get_conn()
        try:
            if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                return False, "Username already exists", "user"

            pwd_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            token = secrets.token_hex(32)
            now = time.time()

            role = 'admin' if email and email.lower() in config.DEFAULT_ADMIN_EMAILS else 'user'
            exp = now + SESSION_TTL

            c.execute(
                "INSERT INTO users(username, email, password_hash, session_token, session_expires_at, created_at, role) VALUES(?,?,?,?,?,?,?)",
                (username, email, pwd_hash, token, exp, now, role),
            )
            c.commit()
            return True, token, role
        except Exception as e:
            return False, str(e), "user"


def login_user(username: str, password: str) -> tuple[bool, str, str]:
    """Login a user. Returns (success, token_or_error, role)."""
    with _lock:
        c = get_conn()
        try:
            row = c.execute("SELECT password_hash, role FROM users WHERE username=?", (username,)).fetchone()
            if not row or not bcrypt.checkpw(password.encode('utf-8'), row["password_hash"].encode('utf-8')):
                return False, "Invalid username or password", "user"

            token = secrets.token_hex(32)
            exp = time.time() + SESSION_TTL
            c.execute("UPDATE users SET session_token=?, session_expires_at=? WHERE username=?", (token, exp, username))
            c.commit()

            role = row["role"] or "user"
            return True, token, role
        except Exception as e:
            return False, str(e), "user"


def login_or_register_google_user(email: str, name: str) -> tuple[bool, str, str, str]:
    """Login or register a Google user. Returns (success, token_or_error, username, role)."""
    with _lock:
        c = get_conn()
        try:
            row = c.execute("SELECT username, role FROM users WHERE email=?", (email,)).fetchone()

            username = ""
            is_new_user = False
            if row:
                username = row["username"]
            else:
                # Fallback: check if they registered manually using their email as username
                row2 = c.execute("SELECT username FROM users WHERE username=?", (email,)).fetchone()
                if row2:
                    username = email
                    c.execute("UPDATE users SET email=? WHERE username=?", (email, username))
                else:
                    # Register new user: base username is email before @
                    base_username = email.split('@')[0]
                    username = base_username
                    counter = 1
                    while True:
                        if not c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                            break
                        username = f"{base_username}{counter}"
                        counter += 1

                    now = time.time()
                    role = 'admin' if email.lower() in config.DEFAULT_ADMIN_EMAILS else 'user'
                    exp = now + SESSION_TTL
                    c.execute(
                        "INSERT INTO users(username, email, password_hash, session_token, session_expires_at, created_at, role) VALUES(?,?,?,?,?,?,?)",
                        (username, email, "", "", exp, now, role),
                    )
                    is_new_user = True

            token = secrets.token_hex(32)
            exp = time.time() + SESSION_TTL
            c.execute("UPDATE users SET session_token=?, session_expires_at=? WHERE username=?", (token, exp, username))
            c.commit()

            if is_new_user:
                role = 'admin' if email.lower() in config.DEFAULT_ADMIN_EMAILS else 'user'
            else:
                role_row = c.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
                role = (role_row["role"] or "user") if role_row else "user"

            return True, token, username, role
        except Exception as e:
            return False, str(e), "", "user"


def get_user_from_token(token: str) -> str | None:
    """Resolve a session token to a username (None if invalid/expired)."""
    with _lock:
        c = get_conn()
        try:
            row = c.execute("SELECT username, session_expires_at FROM users WHERE session_token=?", (token,)).fetchone()
            if row:
                if row["session_expires_at"] and row["session_expires_at"] < time.time():
                    return None
                return row["username"]
            return None
        except Exception:
            return None


def login_admin_fallback() -> tuple[bool, str, str, str]:
    """Fallback admin login bypassing Google auth."""
    with _lock:
        c = get_conn()
        try:
            row = c.execute("SELECT 1 FROM users WHERE username='admin_fallback'").fetchone()
            now = time.time()
            exp = now + SESSION_TTL
            token = secrets.token_hex(32)
            if not row:
                pwd_hash = bcrypt.hashpw(secrets.token_hex(16).encode(), bcrypt.gensalt()).decode()
                c.execute(
                    "INSERT INTO users(username, email, password_hash, session_token, session_expires_at, created_at, role) VALUES(?,?,?,?,?,?,?)",
                    ('admin_fallback', 'admin_fallback@local', pwd_hash, token, exp, now, 'admin'),
                )
            else:
                c.execute("UPDATE users SET session_token=?, session_expires_at=? WHERE username='admin_fallback'", (token, exp))
            c.commit()
            return True, token, 'admin_fallback', 'admin'
        except Exception as e:
            return False, str(e), "", "user"


def get_user_role(username: str) -> str:
    """Get the role for a user. Returns 'admin' or 'user'."""
    with _lock:
        c = get_conn()
        try:
            row = c.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
            return (row["role"] or "user") if row else "user"
        except Exception:
            return "user"


def set_user_role(username: str, role: str) -> bool:
    """Set the role for a user. Only 'admin' and 'user' are valid."""
    if role not in ("admin", "user"):
        return False
    with _lock:
        c = get_conn()
        try:
            existing = c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
            if not existing:
                return False
            c.execute("UPDATE users SET role=? WHERE username=?", (role, username))
            c.commit()
            return True
        except Exception:
            return False


def get_full_user_by_token(token: str) -> dict | None:
    """Get full user info (username, email, role, created_at) from session token."""
    with _lock:
        c = get_conn()
        try:
            row = c.execute(
                "SELECT username, email, role, created_at, session_expires_at FROM users WHERE session_token=?",
                (token,),
            ).fetchone()
            if not row:
                return None
            if row["session_expires_at"] and row["session_expires_at"] < time.time():
                return None
            return {
                "username": row["username"],
                "email": row["email"],
                "role": row["role"] or "user",
                "created_at": row["created_at"],
            }
        except Exception:
            return None


def get_all_users() -> list[dict]:
    """List all users joined with their api_key details."""
    with _lock:
        c = get_conn()
        try:
            rows = c.execute("""
                SELECT u.username, u.email, u.created_at, u.role,
                       k.key, k.rpm_limit, k.expires_at, k.allowed_models,
                       k.token_limit, k.tokens_used, k.token_reset_period, k.token_last_reset
                FROM users u
                LEFT JOIN api_keys k ON u.username = k.owner_username
                ORDER BY u.created_at DESC
            """).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


def delete_user(username: str) -> bool:
    """Delete a user and cascade-delete their keys + rate-limit history."""
    with _lock:
        c = get_conn()
        try:
            cur = c.execute("DELETE FROM users WHERE username=?", (username,))
            if cur.rowcount == 0:
                return False

            keys = c.execute("SELECT key FROM api_keys WHERE owner_username=?", (username,)).fetchall()
            for key_row in keys:
                key = key_row["key"]
                c.execute("DELETE FROM rate_limits WHERE key=?", (key,))
                c.execute("DELETE FROM api_keys WHERE key=?", (key,))

            c.commit()
            return True
        except Exception:
            return False
