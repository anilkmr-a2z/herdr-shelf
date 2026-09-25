"""Decide which tabs to archive, and archive them."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from . import activity, agents, archive, history
from .api import HerdrError
from .util import FileLock, LockBusy, iso, now as utc_now, parse_iso

log = logging.getLogger("shelf")


def decide(tab: dict, panes: list, table: dict, activity_of, idle: timedelta, now: datetime):
    """None when the tab should be archived, otherwise the reason it should not."""
    if tab.get("focused"):
        return "focused"
    if any(p.get("agent_status") == "working" for p in panes):
        return "working"
    agent_panes = [p for p in panes if p.get("agent")]
    if not agent_panes:
        return "no agent pane"
    for p in agent_panes:
        session = p.get("agent_session")
        if not session or not session.get("value"):
            return f"{p['pane_id']}: no session id"
        if session.get("agent") not in table:
            return f"{p['pane_id']}: agent {session.get('agent')!r} is not in the agent table"
        if not agents.valid_session_value(session["agent"], session["value"]):
            return f"{p['pane_id']}: invalid session id"
        last = activity_of(session["agent"], session["value"])
        if last is None:
            return f"{p['pane_id']}: activity unknown"
        if now - last < idle:
            return f"{p['pane_id']}: active {(now - last).days}d ago"
    return None


def gather(client) -> list:
    """[(tab, [panes in that tab])] from one tab.list and one pane.list."""
    tabs = client.call("tab.list").get("tabs", [])
    by_tab = {}
    for p in client.call("pane.list").get("panes", []):
        by_tab.setdefault(p.get("tab_id"), []).append(p)
    return [(t, by_tab.get(t["tab_id"], [])) for t in tabs]


def _terminals(panes: list) -> frozenset:
    return frozenset(p["terminal_id"] for p in panes if p.get("terminal_id"))


def _find(tabs: list, terminals: frozenset):
    for tab, panes in tabs:
        if terminals and _terminals(panes) == terminals:
            return tab, panes
    return None


def _record_presence(data: dict, tabs: list, now: datetime) -> bool:
    """first_seen for every agent session; active-now only for working panes."""
    changed = False
    for _, panes in tabs:
        for p in panes:
            session = p.get("agent_session")
            if not p.get("agent") or not session or not session.get("value"):
                continue
            key = activity.session_key(session["agent"], session["value"])
            if p.get("agent_status") == "working":
                changed = activity.touch(data, key, now) or changed
            else:
                changed = activity.see(data, key, now) or changed
    return changed


def _activity_lookup(records: dict):
    cache = {}

    def activity_of(agent: str, value: str):
        key = activity.session_key(agent, value)
        if key not in cache:
            cache[key] = activity.effective(records.get(key, {}), history.last_activity(agent, value))
        return cache[key]

    return activity_of


def _due(state: Path, interval_minutes: float, now: datetime) -> bool:
    try:
        last = parse_iso((state / "last_sweep").read_text().strip())
    except FileNotFoundError:
        return True
    return last is None or now - last >= timedelta(minutes=interval_minutes)


def _plural(n: int) -> str:
    return f"{n} tab" if n == 1 else f"{n} tabs"


def summary(report: dict):
    if report["mode"] != "live":
        if not report["eligible"]:
            return None
        return f"shelf (dry-run): would archive {_plural(len(report['eligible']))}: {', '.join(report['eligible'])}"
    parts = []
    if report["archived"]:
        parts.append(f"archived {_plural(len(report['archived']))}: {', '.join(report['archived'])}")
    if report["failed"]:
        parts.append(f"{len(report['failed'])} failed, see herdr plugin log")
    return "shelf: " + "; ".join(parts) if parts else None


def _notify(client, report: dict) -> None:
    text = summary(report)
    if not text:
        return
    try:
        client.call("notification.show", {"title": "shelf", "body": text})
    except HerdrError as e:
        log.warning("notification failed: %s", e)


def run(client, cfg: dict, state_dir, table: dict, if_due: bool = False, now: datetime | None = None):
    """One sweep. Returns the report, or None when not due or another sweep is running."""
    now = now or utc_now()
    state = Path(state_dir)
    if if_due and not _due(state, cfg["sweep_interval_minutes"], now):
        return None
    lock = FileLock(state / "sweep.lock")
    try:
        lock.__enter__()
    except LockBusy:  # another sweep is running; any other LockBusy must surface
        return None
    try:
        report = _sweep(client, cfg, state, table, now)
        (state / "last_sweep").write_text(iso(now) + "\n")
    finally:
        lock.__exit__(None, None, None)
    return report


def _sweep(client, cfg: dict, state: Path, table: dict, now: datetime) -> dict:
    store = activity.ActivityStore(state)
    tabs = gather(client)
    store.update(lambda d: _record_presence(d, tabs, now))
    activity_of = _activity_lookup(store.load())
    idle = timedelta(days=cfg["idle_days"])
    report = {"mode": cfg["mode"], "eligible": [], "archived": [], "failed": [], "skipped": []}
    targets = []
    for tab, panes in tabs:
        label = tab.get("label") or tab["tab_id"]
        reason = decide(tab, panes, table, activity_of, idle, now)
        if reason:
            report["skipped"].append((label, reason))
            continue
        report["eligible"].append(label)
        targets.append((label, _terminals(panes)))
    if cfg["mode"] != "live":
        for label in report["eligible"]:
            log.info("dry-run: would archive %s", label)
    else:
        arch = archive.Archive(state)
        for label, terminals in targets:
            activity_of = _activity_lookup(store.load())  # a track hook may have fired meanwhile
            found = _find(gather(client), terminals)
            if found is None:
                report["skipped"].append((label, "tab changed during the sweep"))
                continue
            tab, panes = found
            reason = decide(tab, panes, table, activity_of, idle, now)
            if reason:
                report["skipped"].append((label, reason))
                continue
            try:
                archive_id = archive.archive_tab(client, arch, tab, panes, table, activity_of,
                                                 cfg["keep_transcripts"], now)
            except archive.Skip as e:
                report["skipped"].append((label, str(e)))
                log.info("skipped %s: %s", label, e)
            except (HerdrError, OSError) as e:
                report["failed"].append((label, str(e)))
                log.error("failed to archive %s: %s", label, e)
            else:
                report["archived"].append(label)
                log.info("archived %s as %s", label, archive_id)
    _notify(client, report)
    return report


def archive_now(client, cfg: dict, state_dir, table: dict, tab_id: str, now: datetime | None = None) -> str:
    """Archive one tab immediately, ignoring idle_days and mode."""
    now = now or utc_now()
    state = Path(state_dir)
    with FileLock(state / "sweep.lock", wait_seconds=10.0):
        tabs = gather(client)
        store = activity.ActivityStore(state)
        store.update(lambda d: _record_presence(d, tabs, now))
        activity_of = _activity_lookup(store.load())
        match = next(((t, p) for t, p in tabs if t["tab_id"] == tab_id), None)
        if match is None:
            raise archive.Skip(f"no tab {tab_id}")
        tab, panes = match
        reason = decide(tab, panes, table, activity_of, timedelta(0), now)
        if reason:
            raise archive.Skip(reason)
        return archive.archive_tab(client, archive.Archive(state), tab, panes, table, activity_of,
                                   cfg["keep_transcripts"], now)
