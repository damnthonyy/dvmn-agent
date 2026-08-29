"""Webhook activity log (issue #10) — SQLite-backed store.

Records every authenticated webhook delivery and what the agent did with it, so
"why did the agent not touch this PR?" can be answered in one place instead of
cross-referencing GitHub's Recent Deliveries page with the server logs.

Persistence is a toggle. With ``dashboard.activity_persist`` off (the default)
the same schema and the same queries run against a shared-cache in-memory
database, so the log is simply cleared on restart — the deployment has no
persistent disk today. Turning the flag on points the identical code at a file.

All access goes through a single connection guarded by a lock. That serializes
readers against writers, which is fine at webhook volume and, in memory mode,
doubles as the sentinel connection: a shared-cache in-memory database is dropped
as soon as its *last* connection closes.

Callers on the event loop must use the async helpers at the bottom of this
module: this server also receives the webhooks, and SQLite calls are blocking.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi.concurrency import run_in_threadpool

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

SCHEMA_VERSION = 1

# Shared-cache in-memory database, used when persistence is disabled.
_MEMORY_DSN = "file:dvmn_activity?mode=memory&cache=shared"

# status values
STATUS_RECEIVED = "received"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_IGNORED = "ignored"
STATUS_FAILED = "failed"

# outcome_reason values for STATUS_IGNORED, one per early return in handle_request
REASON_NO_ACTION = "no_action"
REASON_BOT_USER = "bot_user"
REASON_PR_LOGIC_FILTER = "pr_logic_filter"
REASON_UNHANDLED_EVENT = "unhandled_event"

_DEFAULT_RETENTION = 500
_DEFAULT_MAX_PAYLOAD_BYTES = 256 * 1024

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

# Columns exposed through the API. `payload` is deliberately absent: it holds the
# raw GitHub body and is only ever read server-side, by replay.
_PUBLIC_COLUMNS = (
    "id, delivery_id, received_at, event, action, repo_full_name, pr_number, "
    "sender, installation_id, status, outcome_reason, commands_run, "
    "started_at, finished_at, duration_ms, replay_of, payload_bytes"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id     TEXT    NOT NULL UNIQUE,
    received_at     TEXT    NOT NULL,
    event           TEXT    NOT NULL,
    action          TEXT,
    repo_full_name  TEXT,
    pr_number       INTEGER,
    sender          TEXT,
    installation_id INTEGER,
    status          TEXT    NOT NULL,
    outcome_reason  TEXT,
    commands_run    TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    duration_ms     INTEGER,
    replay_of       TEXT,
    payload         TEXT,
    payload_bytes   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_received  ON webhook_events(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_repo_time ON webhook_events(repo_full_name, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_status    ON webhook_events(status);
"""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _setting(key: str, default):
    value = get_settings().get(key, default)
    return default if value is None else value


def is_enabled() -> bool:
    return bool(_setting("DASHBOARD.ACTIVITY_ENABLED", True))


def _persist() -> bool:
    return bool(_setting("DASHBOARD.ACTIVITY_PERSIST", False))


def _db_path() -> str:
    return str(_setting("DASHBOARD.ACTIVITY_DB_PATH", "data/activity.db"))


def _retention() -> int:
    try:
        return max(1, int(_setting("DASHBOARD.ACTIVITY_RETENTION", _DEFAULT_RETENTION)))
    except (TypeError, ValueError):
        return _DEFAULT_RETENTION


def _max_payload_bytes() -> int:
    try:
        return max(0, int(_setting("DASHBOARD.ACTIVITY_MAX_PAYLOAD_BYTES", _DEFAULT_MAX_PAYLOAD_BYTES)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_PAYLOAD_BYTES


# ---------------------------------------------------------------------------
# Connection / migrations
# ---------------------------------------------------------------------------

def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _open() -> sqlite3.Connection:
    if _persist():
        path = _db_path()
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
    else:
        conn = sqlite3.connect(_MEMORY_DSN, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    _migrate(conn)
    return conn


def _connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _open()
    return _conn


def reset_store() -> None:
    """Close and forget the connection. For tests and for settings changes."""
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None


# ---------------------------------------------------------------------------
# Payload summary
# ---------------------------------------------------------------------------

def summarize(body: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the identifying fields out of a webhook body, tolerating any shape."""
    body = body or {}
    pr_number = None
    for path in (
        ("pull_request", "number"),
        ("issue", "number"),
    ):
        node = body.get(path[0])
        if isinstance(node, dict) and isinstance(node.get(path[1]), int):
            pr_number = node[path[1]]
            break
    if pr_number is None:
        check_run = body.get("check_run")
        if isinstance(check_run, dict):
            pulls = check_run.get("pull_requests") or []
            if pulls and isinstance(pulls[0], dict):
                pr_number = pulls[0].get("number")

    def _nested(key, sub):
        node = body.get(key)
        return node.get(sub) if isinstance(node, dict) else None

    return {
        "action": body.get("action"),
        "repo_full_name": _nested("repository", "full_name"),
        "pr_number": pr_number,
        "sender": _nested("sender", "login"),
        "installation_id": _nested("installation", "id"),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode_payload(body: Dict[str, Any]):
    """Serialize the body, dropping it when over the cap. Returns (text, size)."""
    try:
        text = json.dumps(body)
    except (TypeError, ValueError):
        return None, 0
    size = len(text.encode("utf-8"))
    if size > _max_payload_bytes():
        return None, size
    return text, size


# ---------------------------------------------------------------------------
# Writes (blocking — call through the async helpers below)
# ---------------------------------------------------------------------------

def record_received(delivery_id: str, event: str, body: Dict[str, Any],
                    replay_of: Optional[str] = None) -> None:
    """Insert the arrival row. Re-deliveries of the same delivery_id are ignored."""
    fields = summarize(body)
    payload, size = _encode_payload(body)
    with _lock:
        conn = _connection()
        conn.execute(
            """
            INSERT OR IGNORE INTO webhook_events
                (delivery_id, received_at, event, action, repo_full_name, pr_number,
                 sender, installation_id, status, replay_of, payload, payload_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (delivery_id, _now(), event or "", fields["action"], fields["repo_full_name"],
             fields["pr_number"], fields["sender"], fields["installation_id"],
             STATUS_RECEIVED, replay_of, payload, size),
        )
        conn.execute(
            """
            DELETE FROM webhook_events WHERE id NOT IN (
                SELECT id FROM webhook_events ORDER BY id DESC LIMIT ?
            )
            """,
            (_retention(),),
        )
        conn.commit()


def mark_processing(delivery_id: str) -> None:
    with _lock:
        conn = _connection()
        conn.execute(
            "UPDATE webhook_events SET status = ?, started_at = ? WHERE delivery_id = ?",
            (STATUS_PROCESSING, _now(), delivery_id),
        )
        conn.commit()


def mark_outcome(delivery_id: str, status: str, reason: Optional[str] = None,
                 commands: Optional[List[str]] = None) -> None:
    """Close a row: final status, why, and which commands ran."""
    finished = _now()
    with _lock:
        conn = _connection()
        row = conn.execute(
            "SELECT received_at, started_at FROM webhook_events WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        duration = None
        if row:
            started = row["started_at"] or row["received_at"]
            try:
                delta = datetime.fromisoformat(finished) - datetime.fromisoformat(started)
                duration = int(delta.total_seconds() * 1000)
            except (TypeError, ValueError):
                duration = None
        conn.execute(
            """
            UPDATE webhook_events
               SET status = ?, outcome_reason = ?, commands_run = ?,
                   finished_at = ?, duration_ms = ?
             WHERE delivery_id = ?
            """,
            (status, reason, json.dumps(commands) if commands else None,
             finished, duration, delivery_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def list_events(limit: int = 100, repo: Optional[str] = None,
                status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Most recent events first. Never returns the stored payload."""
    limit = max(1, min(int(limit), 500))
    clauses, params = [], []
    if repo:
        clauses.append("repo_full_name = ?")
        params.append(repo)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with _lock:
        rows = _connection().execute(
            f"SELECT {_PUBLIC_COLUMNS} FROM webhook_events {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        event["commands_run"] = json.loads(event["commands_run"]) if event["commands_run"] else []
        event["replayable"] = bool(event["payload_bytes"]) and event["payload_bytes"] <= _max_payload_bytes()
        events.append(event)
    return events


def get_payload(delivery_id: str) -> Optional[Dict[str, Any]]:
    """Read a stored body. Server-side only — never expose this through the API."""
    with _lock:
        row = _connection().execute(
            "SELECT payload FROM webhook_events WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
    if not row or not row["payload"]:
        return None
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return None


def count_events() -> int:
    with _lock:
        return _connection().execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0]


# ---------------------------------------------------------------------------
# Async helpers — the only entry points safe to call from the event loop
# ---------------------------------------------------------------------------

async def arecord_received(delivery_id: str, event: str, body: Dict[str, Any],
                           replay_of: Optional[str] = None) -> None:
    if not delivery_id or not is_enabled():
        return
    try:
        await run_in_threadpool(record_received, delivery_id, event, body, replay_of)
    except Exception as e:
        get_logger().error(f"Activity log: failed to record delivery {delivery_id}: {e}")


async def amark_processing(delivery_id: str) -> None:
    if not delivery_id or not is_enabled():
        return
    try:
        await run_in_threadpool(mark_processing, delivery_id)
    except Exception as e:
        get_logger().error(f"Activity log: failed to mark {delivery_id} processing: {e}")


async def amark_outcome(delivery_id: str, status: str, reason: Optional[str] = None,
                        commands: Optional[List[str]] = None) -> None:
    if not delivery_id or not is_enabled():
        return
    try:
        await run_in_threadpool(mark_outcome, delivery_id, status, reason, commands)
    except Exception as e:
        get_logger().error(f"Activity log: failed to record outcome for {delivery_id}: {e}")


async def alist_events(limit: int = 100, repo: Optional[str] = None,
                       status: Optional[str] = None) -> List[Dict[str, Any]]:
    return await run_in_threadpool(list_events, limit, repo, status)
