"""Decide which tabs to archive, and archive them."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from . import activity, agents, archive, history
from .api import HerdrError
from .util import FileLock, LockBusy, iso, now as utc_now, parse_iso

log = logging.getLogger("shelf")


def decide(tab: dict, panes: list, table: dict, activity_of, idle: timedelta, now: datetime,
           open_in: dict | None = None):
    """None when the tab should be archived, otherwise the reason it should not.

    open_in, when given, maps a session key to the set of tab_ids it is open
    in across the whole sweep; a session open in some tab_id other than this
    tab's own is never archived, so the same conversation cannot end up open
    in two tabs at once. None (the default) skips that check entirely.
    """
    if tab.get("focused"):
        return "focused"
    if any(p.get("agent_status") == "working" for p in panes):
        return "working"
    agent_panes = [p for p in panes if p.get("agent")]
    if not agent_panes:
        return "no agent pane"
    seen_keys = set()
    for p in agent_panes:
        session = p.get("agent_session")
        if not session or not session.get("value"):
            return f"{p['pane_id']}: no session id"
        if session.get("agent") != p.get("agent"):
            # herdr can keep a previous agent's session on a pane where a
            # different agent now runs; that stale session must never be
            # used to decide (or resume) this pane.
            return f"{p['pane_id']}: agent does not match its session"
        if session.get("agent") not in table:
            return f"{p['pane_id']}: agent {session.get('agent')!r} is not in the agent table"
        if not agents.valid_session_value(session["agent"], session["value"]):
            return f"{p['pane_id']}: invalid session id"
        key = activity.session_key(session["agent"], session["value"])
        if key in seen_keys:
            # Two panes of this same tab carrying the same conversation:
            # open_in (keyed by tab_id) never catches this on its own, since
            # both panes belong to this one tab.
            return f"{p['pane_id']}: conversation {session['value'][:8]} is also open in another pane"
        seen_keys.add(key)
        if open_in is not None:
            other_tabs = open_in.get(key, set()) - {tab.get("tab_id")}
            if other_tabs:
                return f"{p['pane_id']}: conversation {session['value'][:8]} is also open in another tab"
        last = activity_of(session["agent"], session["value"], p.get("terminal_id"))
        if last is None:
            return f"{p['pane_id']}: activity unknown"
        if now - last < idle:
            return f"{p['pane_id']}: active {(now - last).days}d ago"
    return None


def _open_sessions(tabs: list) -> dict:
    """Session key -> set of tab_ids an agent pane carries that session in,
    across the whole sweep, so decide() can refuse to archive a conversation
    that is open in more than one tab."""
    result: dict = {}
    for tab, panes in tabs:
        for p in panes:
            session = p.get("agent_session")
            if not p.get("agent") or not session or not session.get("value"):
                continue
            key = activity.session_key(session.get("agent"), session["value"])
            result.setdefault(key, set()).add(tab.get("tab_id"))
    return result


def gather(client) -> list:
    """[(tab, [panes in that tab])] from one tab.list and one pane.list."""
    tabs = client.call("tab.list").get("tabs", [])
    by_tab = {}
    for p in client.call("pane.list").get("panes", []):
        by_tab.setdefault(p.get("tab_id"), []).append(p)
    return [(t, by_tab.get(t["tab_id"], [])) for t in tabs]


def _find(tabs: list, terminals: frozenset):
    for tab, panes in tabs:
        if terminals and archive.pane_terminals(panes) == terminals:
            return tab, panes
    return None


def _record_presence(data: dict, tabs: list, now: datetime) -> bool:
    """first_seen for every agent session; active-now only for working panes.

    Also copies each present agent pane's terminal-recorded agent_started_at
    onto its own session record (keeping the max), so it survives a herdr
    restart, which assigns the pane a new terminal_id and would otherwise
    strand the only copy under a terminal id that is about to be pruned; and
    prunes data["terminals"] down to terminal ids seen in this gather, so a
    terminal_id recorded by track() (pane.agent_detected) does not stick
    around forever once its pane is gone. A terminal entry started at or
    after this sweep's own "now" is kept even if it is not in this gather --
    a concurrent track() call can record a pane that appeared after this
    sweep's tabs were listed, and that must not be pruned just because it
    postdates the snapshot being processed here.
    """
    changed = False
    raw_terminals = data.get(activity.TERMINALS_KEY)
    if raw_terminals is None:
        terminals: dict = {}
    elif isinstance(raw_terminals, dict):
        terminals = raw_terminals
    else:
        data[activity.TERMINALS_KEY] = {}
        terminals = data[activity.TERMINALS_KEY]
        changed = True

    current_terminals = {p["terminal_id"] for _, panes in tabs for p in panes if p.get("terminal_id")}

    for _, panes in tabs:
        for p in panes:
            terminal_id = p.get("terminal_id")
            entry = terminals.get(terminal_id) if terminal_id else None
            if not isinstance(entry, dict):
                continue
            started = parse_iso(entry.get("agent_started_at"))
            session = p.get("agent_session")
            if started is None or not isinstance(session, dict) or not session.get("agent") \
                    or not session.get("value"):
                continue
            key = activity.session_key(session["agent"], session["value"])
            rec = data.setdefault(key, {})
            existing = parse_iso(rec.get("agent_started_at"))
            if existing is None or started > existing:
                rec["agent_started_at"] = entry["agent_started_at"]
                changed = True

    for terminal_id in [t for t in terminals if t not in current_terminals]:
        entry = terminals.get(terminal_id)
        started = parse_iso(entry.get("agent_started_at")) if isinstance(entry, dict) else None
        if started is not None and started >= now:
            continue  # possibly a concurrent write racing with this sweep's own gather
        del terminals[terminal_id]
        changed = True

    for _, panes in tabs:
        for p in panes:
            session = p.get("agent_session")
            if not p.get("agent") or not session or not session.get("value"):
                continue
            key = activity.session_key(session["agent"], session["value"])
            if p.get("agent_status") == "working":
                touched = activity.touch(data, key, now)
                rec = data[key]
                if rec.get("last_status") != "working":
                    # So a following "done" (or "blocked") event is counted
                    # as a status change even if track() never saw the
                    # "working" transition itself (for example the plugin
                    # was not running yet), relying on this backstop instead.
                    rec["last_status"] = "working"
                    touched = True
                changed = touched or changed
            else:
                changed = activity.see(data, key, now) or changed
    return changed


def _activity_lookup(records: dict, installed_at: datetime | None):
    cache = {}
    terminals = records.get(activity.TERMINALS_KEY)
    terminals = terminals if isinstance(terminals, dict) else {}

    def activity_of(agent: str, value: str, terminal_id: str | None = None):
        key = activity.session_key(agent, value)
        if key not in cache:
            cache[key] = activity.effective(records.get(key, {}), history.last_activity(agent, value),
                                             installed_at)
        session_activity = cache[key]
        started = None
        if terminal_id:
            entry = terminals.get(terminal_id)
            if isinstance(entry, dict):
                started = parse_iso(entry.get("agent_started_at"))
        candidates = [c for c in (session_activity, started) if c is not None]
        return max(candidates) if candidates else None

    return activity_of


def is_due(state: Path, interval_minutes: float, now: datetime) -> bool:
    """Whether sweep_interval_minutes has passed since last_sweep.

    Public so a caller (the CLI's `sweep --if-due` hook) can check this
    before loading config at all, using a default interval, and skip loading
    config entirely when a sweep is not due -- config.load's own "unknown
    key(s)" warning would otherwise fire on every hook invocation instead of
    at most once per interval.
    """
    try:
        last = parse_iso((state / "last_sweep").read_text().strip())
    except FileNotFoundError:
        return True
    return last is None or now - last >= timedelta(minutes=interval_minutes)


def _installed_at(state: Path, now: datetime) -> datetime:
    """The install time, recorded once on the first sweep (of any kind) that
    ever runs. Sessions already open at that first sweep get first_seen equal
    to this same instant, so activity.effective can tell them apart from a
    session first seen (and so, from the plugin's point of view, newly
    active) sometime after install.
    """
    path = state / "installed_at"
    try:
        ts = parse_iso(path.read_text().strip())
    except FileNotFoundError:
        ts = None
    if ts is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(iso(now) + "\n")
        return now
    return ts


def _workspace_labels(client) -> dict:
    try:
        workspaces = client.call("workspace.list").get("workspaces", [])
    except HerdrError as e:
        # Display labels are a nicety, not essential: a failed workspace.list
        # must not end the whole sweep. Tab labels alone are used instead.
        log.warning("workspace.list failed; tab labels will not include a workspace name: %s", e)
        return {}
    return {ws.get("workspace_id"): ws.get("label") for ws in workspaces}


def _display_label(tab: dict, workspace_labels: dict) -> str:
    """The tab's own label, unless it is missing or purely numeric (herdr's
    default label is just the tab number), in which case
    "<workspace label>/<tab label or tab_id>" is shown instead so a report or
    notification never shows a bare, meaningless number.
    """
    label = tab.get("label")
    fallback = label or tab["tab_id"]
    if isinstance(label, str) and label and not label.isdigit():
        return label
    ws_label = workspace_labels.get(tab.get("workspace_id"))
    return f"{ws_label}/{fallback}" if ws_label else fallback


def _skip(report: dict, label: str, reason: str) -> None:
    report["skipped"].append((label, reason))
    log.info("skip %s: %s", label, reason)


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


def run(client, cfg: dict, state_dir, table: dict, if_due: bool = False, now: datetime | None = None,
       herdr_session: str = "default"):
    """One sweep. Returns the report, or None when not due or another sweep is running."""
    now = now or utc_now()
    state = Path(state_dir)
    if if_due and not is_due(state, cfg["sweep_interval_minutes"], now):
        return None
    lock = FileLock(state / "sweep.lock")
    try:
        lock.__enter__()
    except LockBusy:  # another sweep is running; any other LockBusy must surface
        return None
    try:
        # Another sweep may have run (and updated last_sweep) between the
        # check above and actually getting the lock; re-check now to avoid
        # double-sweeping right after it.
        if if_due and not is_due(state, cfg["sweep_interval_minutes"], now):
            return None
        report = {"mode": cfg["mode"], "eligible": [], "archived": [], "failed": [], "skipped": []}
        gathered = []
        try:
            _sweep(client, cfg, state, table, now, report, gathered, herdr_session)
            return report
        finally:
            # Notify with whatever the report holds -- even a partial one --
            # so a sweep that failed partway through is still surfaced, and
            # exactly once. last_sweep is only written once the initial
            # gather succeeded: if herdr could not even be listed, the next
            # check (e.g. a focus event) should retry rather than wait out
            # the interval.
            _notify(client, report)
            if gathered:
                (state / "last_sweep").write_text(iso(now) + "\n")
    finally:
        lock.__exit__(None, None, None)


def _sweep(client, cfg: dict, state: Path, table: dict, now: datetime, report: dict, gathered: list,
          herdr_session: str = "default") -> None:
    """Mutate report in place. Appends to gathered once the initial gather succeeds."""
    tabs = gather(client)
    gathered.append(True)
    installed_at = _installed_at(state, now)
    workspace_labels = _workspace_labels(client)
    store = activity.ActivityStore(state)
    store.update(lambda d: _record_presence(d, tabs, now))
    activity_of = _activity_lookup(store.load(), installed_at)
    open_in = _open_sessions(tabs)
    idle = timedelta(days=cfg["idle_days"])
    targets = []
    for tab, panes in tabs:
        label = _display_label(tab, workspace_labels)
        reason = decide(tab, panes, table, activity_of, idle, now, open_in)
        if reason:
            _skip(report, label, reason)
            continue
        report["eligible"].append(label)
        targets.append((label, archive.pane_terminals(panes)))
    if cfg["mode"] != "live":
        for label in report["eligible"]:
            log.info("dry-run: would archive %s", label)
        return
    arch = archive.Archive(state)
    for label, terminals in targets:
        try:
            # Re-gather, re-locate and re-decide inside this target's own try:
            # herdr compacts ids when a tab closes, so a failure here (or in
            # archive_tab below) must be reported as this target's failure
            # and must not abort the rest of the sweep.
            activity_of = _activity_lookup(store.load(), installed_at)  # a track hook may have fired meanwhile
            fresh_tabs = gather(client)
            found = _find(fresh_tabs, terminals)
            if found is None:
                _skip(report, label, "tab changed during the sweep")
                continue
            tab, panes = found
            reason = decide(tab, panes, table, activity_of, idle, now, _open_sessions(fresh_tabs))
            if reason:
                _skip(report, label, reason)
                continue
            archive_id = archive.archive_tab(client, arch, tab, panes, table, activity_of,
                                             cfg["keep_transcripts"], now, herdr_session)
        except archive.Skip as e:
            _skip(report, label, str(e))
        except Exception as e:
            # A single target's failure (herdr error, filesystem error, or
            # anything unexpected) must not abort the rest of the sweep.
            report["failed"].append((label, str(e)))
            log.exception("failed to archive %s", label)
        else:
            report["archived"].append(label)
            log.info("archived %s as %s", label, archive_id)


# How long archive_now() waits for sweep.lock before giving up. A module
# constant (rather than a literal at the call site) so tests can shrink it
# and avoid a slow test when they intentionally hold the lock.
ARCHIVE_NOW_LOCK_WAIT_SECONDS = 10.0


def _warn_if_open_elsewhere(tab_id: str, panes: list, open_in: dict) -> None:
    """Manual archive proceeds even when the conversation is open elsewhere
    (unlike a sweep, which leaves both tabs alone) -- it is an explicit,
    single-tab action -- but warns, since restoring the resulting archive
    entry later will refuse while the other copy is still open."""
    for p in panes:
        if not p.get("agent"):
            continue
        session = p.get("agent_session")
        if not session or not session.get("value"):
            continue
        key = activity.session_key(session.get("agent"), session["value"])
        if open_in.get(key, set()) - {tab_id}:
            log.warning("%s: conversation %s is also open in another tab", tab_id, session["value"][:8])


def archive_now(client, cfg: dict, state_dir, table: dict, tab_id: str, now: datetime | None = None,
                herdr_session: str = "default") -> str:
    """Archive one tab immediately, ignoring idle_days and mode."""
    now = now or utc_now()
    state = Path(state_dir)
    with FileLock(state / "sweep.lock", wait_seconds=ARCHIVE_NOW_LOCK_WAIT_SECONDS):
        tabs = gather(client)
        installed_at = _installed_at(state, now)
        store = activity.ActivityStore(state)
        store.update(lambda d: _record_presence(d, tabs, now))
        activity_of = _activity_lookup(store.load(), installed_at)
        match = next(((t, p) for t, p in tabs if t["tab_id"] == tab_id), None)
        if match is None:
            raise archive.Skip(f"no tab {tab_id}")
        tab, panes = match
        reason = decide(tab, panes, table, activity_of, timedelta(0), now)
        if reason:
            raise archive.Skip(reason)
        _warn_if_open_elsewhere(tab_id, panes, _open_sessions(tabs))
        return archive.archive_tab(client, archive.Archive(state), tab, panes, table, activity_of,
                                   cfg["keep_transcripts"], now, herdr_session)
