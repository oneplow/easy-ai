"""
Knowledge Items (KI) Store — persistent cross-session memory inspired by
Antigravity's Knowledge Items system.

Design:
  - sqlite-backed (reuses the bank/ dir already present)
  - Each KI has: id, title, summary, content, tags, created_at, updated_at
  - Retrieval by keyword search (FTS5) or tag match
  - Distillation: after a session ends, a summary is generated and stored
  - Injection: at session start, relevant KI summaries are prepended to context

Lifecycle (mirrors Antigravity):
  1. Generation  — create new KI from distilled session insights
  2. Consolidation — merge overlapping KIs
  3. Deletion — remove stale/superseded KIs
"""
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from worker import config

log = logging.getLogger("knowledge_store")

_DB_PATH = Path(config.AUTH_DB_PATH).parent / "knowledge.db"
_conn: Optional[sqlite3.Connection] = None

# Re-entrant lock: every DB-touching function must hold this. RLock because
# get_ki_summaries -> list_kis (nested), and apply_distillation -> create_ki /
# update_ki (nested). Without it, concurrent distillation passes + CRUD from
# the admin API could interleave statements on the shared connection and
# corrupt SQLite state.
_lock = threading.RLock()


def _get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            try:
                _conn.execute("PRAGMA journal_mode=WAL")
                _conn.execute("PRAGMA busy_timeout=5000")
            except sqlite3.OperationalError:
                pass
            _init_db(_conn)
        return _conn


def _init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS knowledge_items (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            summary     TEXT NOT NULL DEFAULT '',
            content     TEXT NOT NULL DEFAULT '',
            tags        TEXT NOT NULL DEFAULT '[]',
            scope       TEXT NOT NULL DEFAULT 'global',
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS ki_fts USING fts5(
            title, summary, content, tags,
            content='knowledge_items',
            content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS ki_ai AFTER INSERT ON knowledge_items BEGIN
            INSERT INTO ki_fts(rowid, title, summary, content, tags)
            VALUES (new.rowid, new.title, new.summary, new.content, new.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS ki_ad AFTER DELETE ON knowledge_items BEGIN
            INSERT INTO ki_fts(ki_fts, rowid, title, summary, content, tags)
            VALUES ('delete', old.rowid, old.title, old.summary, old.content, old.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS ki_au AFTER UPDATE ON knowledge_items BEGIN
            INSERT INTO ki_fts(ki_fts, rowid, title, summary, content, tags)
            VALUES ('delete', old.rowid, old.title, old.summary, old.content, old.tags);
            INSERT INTO ki_fts(rowid, title, summary, content, tags)
            VALUES (new.rowid, new.title, new.summary, new.content, new.tags);
        END;

        CREATE TABLE IF NOT EXISTS session_distillations (
            id          TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            ki_id       TEXT,
            action      TEXT NOT NULL,
            created_at  REAL NOT NULL
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------

def create_ki(title: str, summary: str, content: str = "",
              tags: list[str] | None = None, scope: str = "global") -> dict:
    """Create a new Knowledge Item. Returns the created KI dict."""
    with _lock:
        conn = _get_conn()
        ki_id = f"ki_{uuid.uuid4().hex[:12]}"
        now = time.time()
        tags_json = json.dumps(tags or [])
        conn.execute(
            """INSERT INTO knowledge_items (id, title, summary, content, tags, scope, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ki_id, title, summary, content, tags_json, scope, now, now),
        )
        conn.commit()
        log.info("Created KI %s: %s", ki_id, title)
        return {"id": ki_id, "title": title, "summary": summary, "content": content,
                "tags": tags or [], "scope": scope, "created_at": now, "updated_at": now}


def update_ki(ki_id: str, title: str | None = None, summary: str | None = None,
              content: str | None = None, tags: list[str] | None = None) -> bool:
    """Update fields of an existing KI. Returns True if found."""
    with _lock:
        conn = _get_conn()
        row = conn.execute("SELECT * FROM knowledge_items WHERE id = ?", (ki_id,)).fetchone()
        if not row:
            return False
        updates = []
        params = []
        if title is not None:
            updates.append("title = ?"); params.append(title)
        if summary is not None:
            updates.append("summary = ?"); params.append(summary)
        if content is not None:
            updates.append("content = ?"); params.append(content)
        if tags is not None:
            updates.append("tags = ?"); params.append(json.dumps(tags))
        if not updates:
            return True
        updates.append("updated_at = ?"); params.append(time.time())
        params.append(ki_id)
        conn.execute(f"UPDATE knowledge_items SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        log.info("Updated KI %s", ki_id)
        return True


def delete_ki(ki_id: str) -> bool:
    """Delete a KI by id. Returns True if deleted."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM knowledge_items WHERE id = ?", (ki_id,))
        conn.commit()
        return cur.rowcount > 0


def get_ki(ki_id: str) -> dict | None:
    """Get a single KI by id."""
    with _lock:
        conn = _get_conn()
        row = conn.execute("SELECT * FROM knowledge_items WHERE id = ?", (ki_id,)).fetchone()
        if not row:
            return None
        return _row_to_dict(row)


def list_kis(scope: str | None = None, limit: int = 50) -> list[dict]:
    """List all KIs (optionally filtered by scope), most recent first."""
    with _lock:
        conn = _get_conn()
        if scope:
            rows = conn.execute(
                "SELECT * FROM knowledge_items WHERE scope = ? ORDER BY updated_at DESC LIMIT ?",
                (scope, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM knowledge_items ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def search_kis(query: str, limit: int = 10) -> list[dict]:
    """Full-text search across title, summary, content, tags."""
    with _lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                """SELECT ki.* FROM ki_fts fts
                   JOIN knowledge_items ki ON ki.rowid = fts.rowid
                   WHERE ki_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # Malformed FTS query fallback: use LIKE
            like = f"%{query}%"
            rows = conn.execute(
                """SELECT * FROM knowledge_items
                   WHERE title LIKE ? OR summary LIKE ? OR content LIKE ? OR tags LIKE ?
                   ORDER BY updated_at DESC LIMIT ?""",
                (like, like, like, like, limit),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_ki_summaries(limit: int = 20) -> str:
    """Build a text block of KI summaries for injection into the system prompt."""
    kis = list_kis(limit=limit)
    if not kis:
        return ""
    lines = ["<knowledge_items_available>"]
    for ki in kis:
        tags = ", ".join(ki["tags"]) if ki["tags"] else "none"
        lines.append(f"- **{ki['title']}** (tags: {tags}): {ki['summary']}")
    lines.append("</knowledge_items_available>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Distillation — post-session knowledge extraction
# ---------------------------------------------------------------------------

def build_distillation_prompt(session_id: str, messages: list[dict]) -> str:
    """Build a prompt that asks the model to distill session insights into KIs.

    This prompt is sent as a follow-up completion after the user's session ends.
    The model should respond with JSON describing new/updated KIs.
    """
    existing_summaries = get_ki_summaries(limit=30)
    conversation = "\n".join(
        f"{m.get('role', 'user').upper()}: {m.get('content', '')[:500]}"
        for m in messages[-20:]  # last 20 turns max
    )

    return f"""\
You are a Knowledge Distillation agent. Your job is to extract reusable \
insights from the conversation below and produce structured Knowledge Items.

EXISTING KNOWLEDGE (do not duplicate):
{existing_summaries or "(none yet)"}

CONVERSATION TO DISTILL:
{conversation}

INSTRUCTIONS:
1. Identify 0-3 reusable insights (architecture decisions, patterns, gotchas, \
   conventions, important facts about the codebase).
2. For each, decide: CREATE a new KI, UPDATE an existing one, or skip if \
   already covered.
3. Respond with ONLY a JSON array:

[
  {{"action": "create", "title": "...", "summary": "...", "content": "...", "tags": ["..."]}},
  {{"action": "update", "ki_id": "...", "summary": "...", "content": "..."}},
]

If nothing is worth distilling, respond with: []
Do NOT include trivial facts, generic programming knowledge, or one-off debugging steps.
"""


def apply_distillation(result_json: str) -> list[str]:
    """Parse the distillation model response and apply KI operations.
    Returns list of action descriptions."""
    try:
        actions = json.loads(result_json.strip())
    except (json.JSONDecodeError, ValueError):
        # Try to extract JSON array from text
        import re
        match = re.search(r'\[.*\]', result_json, re.DOTALL)
        if not match:
            return []
        try:
            actions = json.loads(match.group())
        except (json.JSONDecodeError, ValueError):
            return []

    if not isinstance(actions, list):
        return []

    results = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        act = action.get("action", "")
        if act == "create":
            title = action.get("title", "").strip()
            summary = action.get("summary", "").strip()
            content = action.get("content", "").strip()
            tags = action.get("tags", [])
            if title and summary:
                ki = create_ki(title, summary, content, tags)
                results.append(f"Created KI: {ki['title']}")
        elif act == "update":
            ki_id = action.get("ki_id", "").strip()
            if ki_id:
                update_ki(
                    ki_id,
                    summary=action.get("summary"),
                    content=action.get("content"),
                    tags=action.get("tags"),
                )
                results.append(f"Updated KI: {ki_id}")
        elif act == "delete":
            ki_id = action.get("ki_id", "").strip()
            if ki_id and delete_ki(ki_id):
                results.append(f"Deleted KI: {ki_id}")

    return results


def record_distillation(session_id: str, ki_id: str | None, action: str):
    """Record that a distillation happened for audit."""
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO session_distillations (id, session_id, ki_id, action, created_at) VALUES (?, ?, ?, ?, ?)",
            (f"dist_{uuid.uuid4().hex[:12]}", session_id, ki_id, action, time.time()),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["tags"] = json.loads(d.get("tags", "[]"))
    except (json.JSONDecodeError, ValueError):
        d["tags"] = []
    return d
