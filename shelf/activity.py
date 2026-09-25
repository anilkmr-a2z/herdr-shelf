"""Per-session activity: what the tracker records, and effective activity."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .util import FileLock, atomic_write_json, iso, parse_iso, read_json

# Statuses that mean the agent did something: ran, asked for input, finished.
# "idle" only means a finished agent was seen; "unknown" means herdr could not tell.
ACTIVE_STATUSES = frozenset({"working", "blocked", "done"})
TOUCH_SKIP = timedelta(seconds=60)

# How long after herdr's own startup a pane.agent_detected event is assumed to
# be herdr resuming a pane it restored itself, rather than the user (re)starting
# an agent by hand -- so that resume must not count as activity.
STARTUP_GRACE = timedelta(minutes=10)

# Reserved top-level key in activity.json for per-terminal data (currently
# just agent_started_at). Every other top-level key is a "<agent>:<value>"
# session record; code that iterates activity.json must skip this one.
TERMINALS_KEY = "terminals"


def session_key(agent: str, value: str) -> str:
    return f"{agent}:{value}"


class ActivityStore:
    """activity.json in the state directory, updated under a lock."""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / "activity.json"
        self.lock_path = Path(state_dir) / "activity.lock"

    def load(self) -> dict:
        data = read_json(self.path, {})
        return data if isinstance(data, dict) else {}

    def update(self, fn) -> bool:
        """Apply fn(data) under the lock; write unless fn returns False.

        Returns whether the write actually happened, so callers can tell
        whether anything changed.
        """
        with FileLock(self.lock_path, wait_seconds=5.0):
            data = self.load()
            changed = fn(data) is not False
            if changed:
                atomic_write_json(self.path, data)
            return changed


def touch(data: dict, key: str, now: datetime) -> bool:
    """Record activity now. Skips the write when the last record is under 60s
    old. A last_active in the future (clock skew) is always overwritten."""
    rec = data.setdefault(key, {})
    changed = "first_seen" not in rec
    rec.setdefault("first_seen", iso(now))
    last = parse_iso(rec.get("last_active"))
    if last is None or last > now or now - last >= TOUCH_SKIP:
        rec["last_active"] = iso(now)
        changed = True
    return changed


def record_status(data: dict, key: str, status: str, now: datetime) -> bool:
    """Record activity for one pane.agent_status_changed event, tracking
    last_status per session. "working" always counts (subject to touch's own
    60s skip); "blocked" and "done" count only when the status changed since
    the last recorded status, because herdr also fires this event when only
    a pane's title or labels change.
    """
    rec = data.setdefault(key, {})
    prev_status = rec.get("last_status")
    status_changed = status != prev_status
    if status != "working" and not status_changed:
        return False
    touched = touch(data, key, now)
    rec["last_status"] = status
    return touched or status_changed


def see(data: dict, key: str, now: datetime) -> bool:
    rec = data.setdefault(key, {})
    if "first_seen" in rec:
        return False
    rec["first_seen"] = iso(now)
    return True


def mark_restored(data: dict, key: str, now: datetime) -> bool:
    rec = data.setdefault(key, {})
    rec.setdefault("first_seen", iso(now))
    rec["restored_at"] = iso(now)
    return True


def effective(rec: dict, history_ts: datetime | None, installed_at: datetime | None = None) -> datetime | None:
    """Latest of last_active, restored_at, and either history or first_seen.

    When a history source has a timestamp, first_seen only counts in
    addition to it when it is strictly later than installed_at -- a session
    seen for the first time after the plugin was installed, whose transcript
    (history) happens to be old, for example a conversation resumed by hand.
    A session first seen at install time (first_seen == installed_at, as
    every session already open on the first sweep is) leaves history alone
    to decide. Without installed_at (unknown), first_seen is not counted
    alongside history, matching that same "seen at install" behavior.
    """
    candidates = [parse_iso(rec.get("last_active")), parse_iso(rec.get("restored_at"))]
    first_seen = parse_iso(rec.get("first_seen"))
    if history_ts is not None:
        candidates.append(history_ts)
        if first_seen is not None and installed_at is not None and first_seen > installed_at:
            candidates.append(first_seen)
    else:
        candidates.append(first_seen)
    present = [c for c in candidates if c is not None]
    return max(present) if present else None


def _server_started_at(state_dir) -> datetime | None:
    try:
        text = (Path(state_dir) / "server_started_at").read_text().strip()
    except FileNotFoundError:
        return None
    return parse_iso(text)


def _in_startup_grace(state_dir, now: datetime) -> bool:
    started = _server_started_at(state_dir)
    return started is not None and now - started < STARTUP_GRACE


def record_terminal_started(data: dict, terminal_id: str, now: datetime) -> bool:
    """Record that an agent (re)started in this pane's terminal, keyed by
    terminal_id rather than session, so a resume with no user or assistant
    message still counts as activity."""
    data.setdefault(TERMINALS_KEY, {})[terminal_id] = {"agent_started_at": iso(now)}
    return True


def _track_agent_detected(client, store: ActivityStore, data: dict, pane_id: str, now: datetime) -> bool:
    """Handle one pane.agent_detected event: a truthy agent that was not
    released means an agent is now running in this pane (started or
    restarted). herdr's own resume of a restored pane, right after herdr
    itself starts, must not count -- that is the startup grace period.
    """
    if not data.get("agent") or data.get("released"):
        return False
    pane = client.call("pane.get", {"pane_id": pane_id}).get("pane") or {}
    terminal_id = pane.get("terminal_id")
    if not terminal_id:
        return False
    if _in_startup_grace(store.path.parent, now):
        return False
    return store.update(lambda d: record_terminal_started(d, terminal_id, now))


def track(client, store: ActivityStore, event_json: str | None, env_pane_id: str | None, now: datetime) -> bool:
    """Handle one pane.agent_status_changed or pane.agent_detected event.

    Returns whether activity.json was actually changed as a result.
    """
    try:
        parsed = json.loads(event_json or "{}")
    except (ValueError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    data = parsed.get("data")
    if not isinstance(data, dict):
        return False
    pane_id = data.get("pane_id") or env_pane_id
    if not pane_id:
        return False
    if parsed.get("event") == "pane.agent_detected":
        return _track_agent_detected(client, store, data, pane_id, now)
    status = data.get("agent_status")
    if status not in ACTIVE_STATUSES:
        return False
    pane = client.call("pane.get", {"pane_id": pane_id}).get("pane") or {}
    session = pane.get("agent_session")
    if not isinstance(session, dict) or not session.get("agent") or not session.get("value"):
        return False
    key = session_key(session["agent"], session["value"])
    return store.update(lambda d: record_status(d, key, status, now))
