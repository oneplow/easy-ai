"""
Usage logging, stats aggregation, and request-log retrieval.

Tracks per-(date, username, model) usage rolls-ups plus the raw request_logs
table used by the dashboard's per-minute model-status heatmap.

All functions acquire worker.auth.db._lock and use the shared connection.
"""
import datetime
import time

from .db import _lock, get_conn
from .api_keys import get_username_from_key


def log_usage(client_key: str, model: str, tokens: int, is_success: bool, latency_ms: int) -> None:
    """Upsert the daily usage roll-up for a (date, username, model) triple."""
    with _lock:
        c = get_conn()
        try:
            row = c.execute("SELECT owner_username FROM api_keys WHERE key=?", (client_key,)).fetchone()
            if not row or not row["owner_username"]:
                return
            username = row["owner_username"]

            date_str = datetime.datetime.now().strftime("%Y-%m-%d")
            success_int = 1 if is_success else 0

            c.execute("""
                INSERT INTO usage_logs(date, username, model, requests, tokens, success, total_latency_ms)
                VALUES(?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(date, username, model) DO UPDATE SET
                    requests = requests + 1,
                    tokens = tokens + ?,
                    success = success + ?,
                    total_latency_ms = total_latency_ms + ?
            """, (date_str, username, model, tokens, success_int, latency_ms, tokens, success_int, latency_ms))

            c.commit()
        except Exception:
            pass


def get_usage_stats(username: str, days: int = 90) -> list[dict]:
    """Get usage stats for a specific user for the last N days."""
    with _lock:
        c = get_conn()
        try:
            cutoff_date = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
            rows = c.execute("""
                SELECT date, model, SUM(requests) as requests, SUM(tokens) as tokens,
                       SUM(success) as success, SUM(total_latency_ms) as total_latency_ms
                FROM usage_logs
                WHERE username=? AND date >= ?
                GROUP BY date, model
                ORDER BY date ASC
            """, (username, cutoff_date)).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


def admin_get_usage_stats(days: int = 90) -> list[dict]:
    """Get total usage stats for all users for the last N days."""
    with _lock:
        c = get_conn()
        try:
            cutoff_date = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
            rows = c.execute("""
                SELECT date, model, SUM(requests) as requests, SUM(tokens) as tokens,
                       SUM(success) as success, SUM(total_latency_ms) as total_latency_ms
                FROM usage_logs
                WHERE date >= ?
                GROUP BY date, model
                ORDER BY date ASC
            """, (cutoff_date,)).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


def insert_request_log(key: str, req_id: str, model: str, method: str, url: str,
                       is_success: bool, input_tokens: int, output_tokens: int,
                       latency_ms: int) -> None:
    """Insert a raw request log row + prune rows older than 7 days."""
    username = get_username_from_key(key)
    if not username:
        return
    with _lock:
        c = get_conn()
        try:
            c.execute('''
                INSERT INTO request_logs(id, username, model, method, url, is_success, input_tokens, output_tokens, latency_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (req_id, username, model, method, url, 1 if is_success else 0, input_tokens, output_tokens, latency_ms, time.time()))

            cutoff = time.time() - (7 * 24 * 60 * 60)
            c.execute('DELETE FROM request_logs WHERE created_at < ?', (cutoff,))

            c.commit()
        except Exception:
            pass


def _merge_prompt_logs(logs: list[dict], window_seconds: int = 90) -> list[dict]:
    """Merge nearby gateway calls from one prompt into a single display row."""
    merged: list[dict] = []

    for log in logs:
        if not merged:
            merged.append({**log, "request_count": 1})
            continue

        last = merged[-1]
        same_user = last.get("username") == log.get("username")
        same_model = last.get("model") == log.get("model")
        same_route = last.get("method") == log.get("method") and last.get("url") == log.get("url")
        close_enough = abs(float(last.get("created_at", 0)) - float(log.get("created_at", 0))) <= window_seconds

        if same_user and same_model and same_route and close_enough:
            last["input_tokens"] = int(last.get("input_tokens") or 0) + int(log.get("input_tokens") or 0)
            last["output_tokens"] = int(last.get("output_tokens") or 0) + int(log.get("output_tokens") or 0)
            last["latency_ms"] = int(last.get("latency_ms") or 0) + int(log.get("latency_ms") or 0)
            last["is_success"] = bool(last.get("is_success")) and bool(log.get("is_success"))
            last["request_count"] = int(last.get("request_count") or 1) + 1
        else:
            merged.append({**log, "request_count": 1})

    return merged


def _row_to_log(r) -> dict:
    return {
        "id": r[0],
        "username": r[1],
        "model": r[2],
        "method": r[3],
        "url": r[4],
        "is_success": bool(r[5]),
        "input_tokens": r[6],
        "output_tokens": r[7],
        "latency_ms": r[8],
        "created_at": r[9],
    }


def get_request_logs(username: str, limit: int = 50, offset: int = 0) -> dict:
    with _lock:
        c = get_conn()
        try:
            rows = c.execute(
                'SELECT id, username, model, method, url, is_success, input_tokens, output_tokens, latency_ms, created_at FROM request_logs WHERE username = ? ORDER BY created_at DESC LIMIT ? OFFSET ?',
                (username, limit, offset),
            ).fetchall()

            total = c.execute('SELECT COUNT(*) FROM request_logs WHERE username = ?', (username,)).fetchone()[0]

            logs = [_row_to_log(r) for r in rows]
            merged_logs = _merge_prompt_logs(logs)

            return {"logs": merged_logs, "total": total}
        except Exception:
            return {"logs": [], "total": 0}


def admin_get_request_logs(limit: int = 50, offset: int = 0) -> dict:
    with _lock:
        c = get_conn()
        try:
            rows = c.execute(
                'SELECT id, username, model, method, url, is_success, input_tokens, output_tokens, latency_ms, created_at FROM request_logs ORDER BY created_at DESC LIMIT ? OFFSET ?',
                (limit, offset),
            ).fetchall()

            total = c.execute('SELECT COUNT(*) FROM request_logs').fetchone()[0]

            logs = [_row_to_log(r) for r in rows]
            merged_logs = _merge_prompt_logs(logs)

            return {"logs": merged_logs, "total": total}
        except Exception:
            return {"logs": [], "total": 0}


def get_model_status_blocks(time_window_minutes: int = 60) -> dict[str, list[int]]:
    """
    Aggregate request_logs into per-minute status blocks for each model.

    Returns a dict: { model_id: [block0, block1, ..., block59] }
    Each block is:
      1 = Healthy  (success rate >= 90% or no traffic)
      3 = Degraded  (50-89%)
      2 = Warning   (20-49%)
      0 = Down      (< 20%)
    block0 is the oldest minute, block[-1] is the most recent.
    """
    now = time.time()
    window_start = now - (time_window_minutes * 60)

    with _lock:
        c = get_conn()
        try:
            rows = c.execute(
                """
                SELECT model,
                       CAST((created_at - ?) / 60 AS INTEGER) AS minute_bucket,
                       COUNT(*) AS total,
                       SUM(CASE WHEN is_success = 1 THEN 1 ELSE 0 END) AS successes
                FROM request_logs
                WHERE created_at >= ?
                GROUP BY model, minute_bucket
                """,
                (window_start, window_start),
            ).fetchall()
        except Exception:
            return {}

    model_minutes: dict[str, dict[int, tuple[int, int]]] = {}
    for r in rows:
        model_id = r[0]
        bucket = r[1]
        total = r[2]
        successes = r[3]
        if model_id not in model_minutes:
            model_minutes[model_id] = {}
        model_minutes[model_id][bucket] = (total, successes)

    result: dict[str, list[int]] = {}
    for model_id, minutes in model_minutes.items():
        blocks: list[int] = []
        for i in range(time_window_minutes):
            if i in minutes:
                total, successes = minutes[i]
                rate = successes / total if total > 0 else 1.0
                if rate >= 0.9:
                    blocks.append(1)   # Healthy
                elif rate >= 0.5:
                    blocks.append(3)   # Degraded (light green)
                elif rate >= 0.2:
                    blocks.append(2)   # Warning (orange)
                else:
                    blocks.append(0)   # Down (red)
            else:
                blocks.append(1)  # No traffic = assume healthy
        result[model_id] = blocks

    return result
