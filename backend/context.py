"""
Conversation memory + agentic session management.

Each session maintains:
  - Turn history (role-tagged messages with rolling char cap)
  - Current mode (PLANNING / EXECUTION / VERIFICATION)
  - Knowledge Items injection (summaries of persistent cross-session knowledge)

Storage strategy (Phase 3 rewrite):
  - SQLite-backed (`bank/context.db`) so history survives restarts.
  - A process-local LRU cache sits in front for hot-session latency.
  - Per-session turn cap (CONTEXT_MAX_PER_SESSION) bounds growth.
  - A periodic TTL sweep (lifecycle.housekeeping_loop -> sweep_expired)
    drops idle sessions older than CONTEXT_TTL_SEC.

Thread safety: every DB-touching path acquires _lock (re-entrant), mirroring
the pattern in worker.auth. The cache is also guarded by the same lock, so
cache + DB never diverge under concurrent writers.

The public API is unchanged from the old in-memory version, so callers in the
chat router and knowledge distillation path need no edits.
"""
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Dict, List

from worker import config

log = logging.getLogger("context")

# --- Knobs (with sensible defaults if config hasn't grown them yet) ---------
MAX_HISTORY_CHARS = getattr(config, "MAX_HISTORY_CHARS", 6000)
_TTL_SEC = int(getattr(config, "CONTEXT_TTL_SEC", 86400))           # 24h
_MAX_PER_SESSION = int(getattr(config, "CONTEXT_MAX_PER_SESSION", 50))
_MAX_SESSIONS = int(getattr(config, "CONTEXT_MAX_SESSIONS", 1000))

_DB_PATH = os.path.join(os.path.dirname(config.AUTH_DB_PATH) or "bank", "context.db")
VALID_MODES = ("PLANNING", "EXECUTION", "VERIFICATION")

# One serializer for DB + cache. RLock because append() may call helpers that
# also touch the store.
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

# Hot-session LRU cache: session_id -> list[{"role","content"}].
# Bounded by _MAX_SESSIONS; least-recently-used evicted back to DB only.
_CACHE: "OrderedDict[str, List[dict]]" = OrderedDict()
# session_id -> current agentic mode (kept in memory + mirrored to DB).
_MODE: Dict[str, str] = {}


# ---------------------------------------------------------------------------
# DB init + connection
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _open_fresh()
    return _conn


def _open_fresh() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(_DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
    except sqlite3.OperationalError:
        pass
    c.execute("""
        CREATE TABLE IF NOT EXISTS session_turns(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            created_at  REAL NOT NULL
        )""")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_turns_session ON session_turns(session_id, id)"
    )
    c.execute("""
        CREATE TABLE IF NOT EXISTS session_meta(
            session_id  TEXT PRIMARY KEY,
            mode        TEXT NOT NULL DEFAULT 'EXECUTION',
            updated_at  REAL NOT NULL
        )""")
    c.commit()
    return c


def init_store() -> None:
    """Create the DB + run migrations. Called once at app startup."""
    with _lock:
        _get_conn()
    log.info(
        "context store ready (path=%s, ttl=%ds, max_turns=%d, max_sessions=%d)",
        _DB_PATH, _TTL_SEC, _MAX_PER_SESSION, _MAX_SESSIONS,
    )


# ---------------------------------------------------------------------------
# History management (cache-first, DB-backed)
# ---------------------------------------------------------------------------

def _load_history(session_id: str) -> List[dict]:
    """Read a session's full history from DB (cache miss path)."""
    c = _get_conn()
    rows = c.execute(
        "SELECT role, content FROM session_turns WHERE session_id=? ORDER BY id",
        (session_id,),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def _touch_cache(session_id: str) -> None:
    """Mark a session as most-recently-used; enforce the cache size cap."""
    _CACHE.move_to_end(session_id)
    while len(_CACHE) > _MAX_SESSIONS:
        _CACHE.popitem(last=False)  # evict LRU (still safe in DB)


def get_history(session_id: str) -> List[dict]:
    """Return the full role-tagged history for a session (cache-first)."""
    with _lock:
        if session_id in _CACHE:
            _touch_cache(session_id)
            return _CACHE[session_id]
        history = _load_history(session_id)
        _CACHE[session_id] = history
        _touch_cache(session_id)
        return history


def append(session_id: str, role: str, content: str) -> None:
    """Append a turn and persist it. Trims to CONTEXT_MAX_PER_SESSION turns."""
    with _lock:
        c = _get_conn()
        now = time.time()
        c.execute(
            "INSERT INTO session_turns(session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, now),
        )
        # Bump session_meta updated_at (used by the TTL sweep).
        c.execute("""
            INSERT INTO session_meta(session_id, mode, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at
        """, (session_id, _MODE.get(session_id, "EXECUTION"), now))
        c.commit()

        # Mirror into cache (load first if absent so the cache is the source of
        # truth for in-process readers).
        if session_id not in _CACHE:
            _CACHE[session_id] = _load_history(session_id)
        _CACHE[session_id].append({"role": role, "content": content})

        # Per-session turn cap: drop oldest turns from both DB and cache so the
        # two stay consistent.
        overflow = len(_CACHE[session_id]) - _MAX_PER_SESSION
        if overflow > 0:
            # Find the oldest N turn ids for this session and delete them.
            oldest_ids = [
                r["id"] for r in c.execute(
                    "SELECT id FROM session_turns WHERE session_id=? ORDER BY id LIMIT ?",
                    (session_id, overflow),
                ).fetchall()
            ]
            if oldest_ids:
                placeholders = ",".join("?" * len(oldest_ids))
                c.execute(
                    f"DELETE FROM session_turns WHERE id IN ({placeholders})",
                    oldest_ids,
                )
                c.commit()
            del _CACHE[session_id][:overflow]
        _touch_cache(session_id)


def reset(session_id: str) -> None:
    """Drop all history + mode for a session (DB + cache)."""
    with _lock:
        c = _get_conn()
        c.execute("DELETE FROM session_turns WHERE session_id=?", (session_id,))
        c.execute("DELETE FROM session_meta WHERE session_id=?", (session_id,))
        c.commit()
        _CACHE.pop(session_id, None)
        _MODE.pop(session_id, None)


# ---------------------------------------------------------------------------
# Mode management (PLANNING / EXECUTION / VERIFICATION)
# ---------------------------------------------------------------------------

def _load_mode(session_id: str) -> str:
    c = _get_conn()
    row = c.execute("SELECT mode FROM session_meta WHERE session_id=?", (session_id,)).fetchone()
    return row["mode"] if row else "EXECUTION"


def get_mode(session_id: str) -> str:
    with _lock:
        if session_id not in _MODE:
            _MODE[session_id] = _load_mode(session_id)
        return _MODE[session_id]


def set_mode(session_id: str, mode: str) -> str:
    """Set session mode. Returns the mode actually set."""
    mode = mode.upper()
    if mode not in VALID_MODES:
        mode = "EXECUTION"
    with _lock:
        _MODE[session_id] = mode
        c = _get_conn()
        c.execute("""
            INSERT INTO session_meta(session_id, mode, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET mode=excluded.mode, updated_at=excluded.updated_at
        """, (session_id, mode, time.time()))
        c.commit()
        return mode


# ---------------------------------------------------------------------------
# Message building with KI injection
# ---------------------------------------------------------------------------

def build_messages(session_id: str, new_message: str,
                   inject_knowledge: bool = True) -> list:
    """Role-tagged history + the new user turn, as [{role, content}] (OpenAI-style).

    When inject_knowledge=True and this is the first turn (or knowledge is
    available), prepends a system message with relevant KI summaries so the
    model has persistent context without needing full conversation logs.
    """
    msgs = []

    if inject_knowledge:
        ki_block = _get_ki_context(session_id)
        if ki_block:
            msgs.append({"role": "system", "content": ki_block})

    msgs.extend(_trim(get_history(session_id)))
    msgs.append({"role": "user", "content": new_message})
    return msgs


def build_prompt(session_id: str, new_message: str) -> str:
    """Serialize history + new message into one self-contained prompt (legacy)."""
    history = _trim(get_history(session_id))
    if not history:
        return new_message
    lines = ["[Previous conversation]"]
    for turn in history:
        speaker = "User" if turn["role"] == "user" else "Assistant"
        lines.append(f"{speaker}: {turn['content']}")
    lines.append("\n[Now respond only to this latest message]")
    lines.append(f"User: {new_message}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Knowledge Items injection
# ---------------------------------------------------------------------------

def _get_ki_context(session_id: str) -> str:
    """Build a KI context block for injection into the conversation.

    Only injects on the first user turn of a session (when history is empty)
    or when explicitly requested.
    """
    history = get_history(session_id)
    if history and len(history) > 1:
        return ""

    try:
        from backend.knowledge_store import get_ki_summaries
        summaries = get_ki_summaries(limit=15)
        if not summaries:
            return ""
        mode = get_mode(session_id)
        return (
            f"<session_context>\n"
            f"Current mode: {mode}\n\n"
            f"{summaries}\n"
            f"</session_context>"
        )
    except Exception as e:
        log.debug("KI injection skipped: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Trimming
# ---------------------------------------------------------------------------

def _trim(history: List[dict]) -> List[dict]:
    """Rolling window: drop oldest turns until under the char budget."""
    total = sum(len(t["content"]) for t in history)
    out = list(history)
    while out and total > MAX_HISTORY_CHARS:
        total -= len(out[0]["content"])
        out = out[1:]
    return out


# ---------------------------------------------------------------------------
# TTL sweep (called periodically from lifecycle.housekeeping_loop)
# ---------------------------------------------------------------------------

def sweep_expired() -> int:
    """Delete sessions whose last activity is older than CONTEXT_TTL_SEC.

    Returns the number of sessions pruned. Safe to call from any thread.
    """
    cutoff = time.time() - _TTL_SEC
    with _lock:
        c = _get_conn()
        stale = [r["session_id"] for r in c.execute(
            "SELECT session_id FROM session_meta WHERE updated_at < ?", (cutoff,)
        ).fetchall()]
        if not stale:
            return 0
        c.executemany("DELETE FROM session_turns WHERE session_id=?", [(s,) for s in stale])
        c.executemany("DELETE FROM session_meta WHERE session_id=?", [(s,) for s in stale])
        c.commit()
        for s in stale:
            _CACHE.pop(s, None)
            _MODE.pop(s, None)
        return len(stale)
