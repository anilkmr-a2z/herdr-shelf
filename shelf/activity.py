"""Per-session activity: what the tracker records, and effective activity."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from .util import FileLock, atomic_write_json, iso, parse_iso, read_json

# Statuses that mean the agent did something: ran, asked for input, finished.
# "idle" only means a finished agent was seen; "unknown" means herdr could not tell.
ACTIVE_STATUSES = frozenset({"working", "blocked", "done"})
TOUCH_SKIP = timedelta(seconds=60)

# On a pane.agent_detected event, herdr often has not yet learned the pane's
# agent_session -- for Claude that comes from its own startup hook, which
# tends to run just after the agent itself is detected -- so pane.get is
# retried this many times, sleeping this long between attempts, waiting for
# a session whose agent matches before giving up. Module constants so tests
# can set the delay to 0.
AGENT_SESSION_RETRY_ATTEMPTS = 10
AGENT_SESSION_RETRY_DELAY_SECONDS = 0.5

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
    """Latest of last_active, restored_at, agent_started_at, and either
    history or first_seen.

    agent_started_at (set by track() on a pane.agent_detected event, and by a
    sweep's _record_presence copying it over from the pane's terminal, so it
    survives a herdr restart that changes terminal ids -- see sweep.py)
    always counts, the same as last_active and restored_at: starting or
    resuming an agent is activity regardless of what a history source or
    first_seen says.

    When a history source has a timestamp, first_seen only counts in
    addition to it when it is strictly later than installed_at -- a session
    seen for the first time after the plugin was installed, whose transcript
    (history) happens to be old, for example a conversation resumed by hand.
    A session first seen at install time (first_seen == installed_at, as
    every session already open on the first sweep is) leaves history alone
    to decide. Without installed_at (unknown), first_seen is not counted
    alongside history, matching that same "seen at install" behavior.
    """
    candidates = [parse_iso(rec.get("last_active")), parse_iso(rec.get("restored_at")),
                  parse_iso(rec.get("agent_started_at"))]
    first_seen = parse_iso(rec.get("first_seen"))
    if history_ts is not None:
        candidates.append(history_ts)
        if first_seen is not None and installed_at is not None and first_seen > installed_at:
            candidates.append(first_seen)
    else:
        candidates.append(first_seen)
    present = [c for c in candidates if c is not None]
    return max(present) if present else None


def _terminals_dict(data: dict) -> dict:
    """data["terminals"], resetting a present-but-invalid value (anything
    other than a dict) to {} rather than letting a corrupt or unexpected
    value crash a later write."""
    terminals = data.get(TERMINALS_KEY)
    if not isinstance(terminals, dict):
        terminals = {}
        data[TERMINALS_KEY] = terminals
    return terminals


def record_terminal_started(data: dict, terminal_id: str, now: datetime) -> bool:
    """Record that an agent (re)started in this pane's terminal, keyed by
    terminal_id rather than session, so a resume with no user or assistant
    message still counts as activity. A sweep's _record_presence later
    copies this into the session's own record too, so it survives a herdr
    restart that assigns the pane a new terminal_id (see sweep.py)."""
    _terminals_dict(data)[terminal_id] = {"agent_started_at": iso(now)}
    return True


def _mark_session_started(rec: dict, now: datetime) -> bool:
    existing = parse_iso(rec.get("agent_started_at"))
    if existing is not None and existing >= now:
        return False
    rec["agent_started_at"] = iso(now)
    return True


def _matching_session_key(pane: dict, agent: str) -> str | None:
    """The pane's agent_session key, but only when that session's own agent
    equals agent -- a stale session left over from a previous, different
    agent in this pane must never be marked as this agent's activity."""
    session = pane.get("agent_session")
    if isinstance(session, dict) and session.get("agent") == agent and session.get("value"):
        return session_key(session["agent"], session["value"])
    return None


def _track_agent_detected(client, store: ActivityStore, data: dict, pane_id: str, now: datetime) -> bool:
    """Handle one pane.agent_detected event: a truthy agent that was not
    released means an agent is now running in this pane (started or
    restarted, including a conversation resumed by hand). herdr omits
    "released" entirely when it is false, and omits "agent" on a release, so
    a release never looks like a start.

    herdr often has not yet learned the pane's agent_session at the moment
    this event fires -- for Claude, that comes from its own startup hook,
    which tends to run just after the agent itself is detected -- so pane.get
    is retried (see AGENT_SESSION_RETRY_ATTEMPTS/_DELAY_SECONDS) until a
    session whose agent matches appears, or the retries are exhausted. The
    terminal itself is always recorded either way.
    """
    agent = data.get("agent")
    if not agent or data.get("released"):
        return False
    pane = client.call("pane.get", {"pane_id": pane_id}).get("pane") or {}
    terminal_id = pane.get("terminal_id")
    if not terminal_id:
        return False
    key = _matching_session_key(pane, agent)
    for _ in range(AGENT_SESSION_RETRY_ATTEMPTS):
        if key is not None:
            break
        time.sleep(AGENT_SESSION_RETRY_DELAY_SECONDS)
        pane = client.call("pane.get", {"pane_id": pane_id}).get("pane") or {}
        key = _matching_session_key(pane, agent)

    def apply(d: dict) -> bool:
        changed = record_terminal_started(d, terminal_id, now)
        if key is not None:
            changed = _mark_session_started(d.setdefault(key, {}), now) or changed
        return changed

    return store.update(apply)


def _is_agent_detected_event(env_event: str | None, json_event, data: dict) -> bool:
    """Tell a pane.agent_detected event apart from pane.agent_status_changed.

    herdr's HERDR_PLUGIN_EVENT (the hook's own dot-form event name) is
    "pane.agent_detected" for this hook. The event *payload* itself
    (HERDR_PLUGIN_EVENT_JSON) uses herdr's snake_case EventKind names instead
    -- its own "event" field, and the "type" field inside "data", are both
    "pane_agent_detected" -- so all three are checked; any one matching is
    enough.
    """
    if env_event == "pane.agent_detected":
        return True
    if json_event in ("pane_agent_detected", "pane.agent_detected"):
        return True
    return data.get("type") == "pane_agent_detected"


def track(client, store: ActivityStore, event_json: str | None, env_pane_id: str | None, now: datetime,
          env_event: str | None = None) -> bool:
    """Handle one pane.agent_status_changed or pane.agent_detected event.

    env_event is HERDR_PLUGIN_EVENT (the hook's own event name, in dot form);
    see _is_agent_detected_event for why the event JSON payload alone is not
    always enough to tell the two events apart.

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
    if _is_agent_detected_event(env_event, parsed.get("event"), data):
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
