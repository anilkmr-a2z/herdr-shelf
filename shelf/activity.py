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

    def update(self, fn) -> None:
        """Apply fn(data) under the lock; write unless fn returns False."""
        with FileLock(self.lock_path, wait_seconds=5.0):
            data = self.load()
            if fn(data) is not False:
                atomic_write_json(self.path, data)


def touch(data: dict, key: str, now: datetime) -> bool:
    """Record activity now. Skips the write when the last record is under 60s old."""
    rec = data.setdefault(key, {})
    changed = "first_seen" not in rec
    rec.setdefault("first_seen", iso(now))
    last = parse_iso(rec.get("last_active"))
    if last is None or now - last >= TOUCH_SKIP:
        rec["last_active"] = iso(now)
        changed = True
    return changed


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


def effective(rec: dict, history_ts: datetime | None) -> datetime | None:
    """Latest of last_active, restored_at, and either history or first_seen."""
    candidates = [parse_iso(rec.get("last_active")), parse_iso(rec.get("restored_at"))]
    candidates.append(history_ts if history_ts is not None else parse_iso(rec.get("first_seen")))
    present = [c for c in candidates if c is not None]
    return max(present) if present else None


def track(client, store: ActivityStore, event_json: str | None, env_pane_id: str | None, now: datetime) -> bool:
    """Handle one pane.agent_status_changed event. Returns True if activity was recorded."""
    try:
        data = json.loads(event_json or "{}").get("data") or {}
    except (ValueError, AttributeError):
        return False
    status = data.get("agent_status")
    pane_id = data.get("pane_id") or env_pane_id
    if status not in ACTIVE_STATUSES or not pane_id:
        return False
    pane = client.call("pane.get", {"pane_id": pane_id}).get("pane") or {}
    session = pane.get("agent_session")
    if not session or not session.get("agent") or not session.get("value"):
        return False
    key = session_key(session["agent"], session["value"])
    store.update(lambda d: touch(d, key, now))
    return True
