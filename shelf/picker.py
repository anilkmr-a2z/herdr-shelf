"""The popup that lists archived tabs and restores one.

The picker logic is pure (State, reduce, render, apply) so it is tested
without a terminal; run() is a thin curses loop around it.
"""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import NamedTuple, Optional

from .util import FileLock, LockBusy, parse_iso

# How long the picker's delete waits for sweep.lock before giving up. A
# module constant (rather than a literal at the call site) so tests can
# shrink it and avoid a slow test when they intentionally hold the lock.
DELETE_LOCK_WAIT_SECONDS = 30.0

LIST_ROWS = 10
TODAY, WEEK, MONTH, OLDER = "Archived today", "Last 7 days", "Last 30 days", "Older"
GROUPS = (TODAY, WEEK, MONTH, OLDER)
KEYS_HINT = " Enter restore  Right/Left open/close  / filter  d delete  q quit"
SWEEP_BUSY = "A sweep is running; try again in a moment."
TOO_SMALL = "Popup too small"
_VIM_KEYS = {"k": "up", "j": "down", "h": "left", "l": "right"}
_STEPS = {"up": -1, "down": 1, "pgup": -LIST_ROWS, "pgdn": LIST_ROWS, "wheelup": -3, "wheeldown": 3}


def days_idle(record: dict, now: datetime):
    stamps = [parse_iso(m.get("last_activity")) for m in record.get("panes", {}).values()]
    stamps = [s for s in stamps if s is not None]
    return (now - max(stamps)).days if stamps else None


def record_label(record: dict) -> str:
    return record.get("tab", {}).get("label") or record.get("workspace", {}).get("label") or "tab"


def agent_names(record: dict) -> str:
    return ",".join(sorted({m["agent"] for m in record.get("panes", {}).values() if m.get("agent")}))


def group_of(record: dict, now: datetime, tz: Optional[tzinfo] = None) -> str:
    """The record's group, by local calendar days since archived_at.

    `now` is aware local time. `tz` is the zone to read archived_at in; None
    means the machine's own, with the DST offset of each timestamp's own date.
    """
    at = parse_iso(record.get("archived_at"))
    if at is None:
        return OLDER
    days = (now.date() - at.astimezone(tz).date()).days
    if days <= 0:  # today, or a clock that ran ahead
        return TODAY
    if days < 7:
        return WEEK
    if days < 30:
        return MONTH
    return OLDER


def _sort_key(record: dict, now: datetime):
    at = parse_iso(record.get("archived_at"))
    idle = days_idle(record, now)
    return (-(at.timestamp() if at else 0.0), float("inf") if idle is None else idle)


def _matches(record: dict, text: str) -> bool:
    text = text.lower()
    fields = (record.get("tab", {}).get("label") or "", record.get("workspace", {}).get("label") or "",
              agent_names(record))
    return any(text in f.lower() for f in fields)


class Row(NamedTuple):
    kind: str  # "header" or "tab"
    group: str
    record: Optional[dict] = None
    count: int = 0
    is_open: bool = False


@dataclass
class State:
    now: datetime
    tz: Optional[tzinfo] = None  # None: the machine's zone (see group_of)
    grouped: dict = field(default_factory=dict)  # group name -> its records, sorted
    open_groups: set = field(default_factory=set)
    saved_open: Optional[set] = None  # open_groups from before the filter was set
    cursor: int = 0  # index into rows(state)
    offset: int = 0  # first row shown in the list window
    filter_text: str = ""
    mode: str = "move"  # "move", "filter" or "confirm"
    message: str = ""
    height: int = 16
    width: int = 80

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.grouped.values())


def _load(state: State, records: list) -> None:
    state.grouped = {g: [] for g in GROUPS}
    for rec in sorted(records, key=lambda r: _sort_key(r, state.now)):
        state.grouped[group_of(rec, state.now, state.tz)].append(rec)


def initial_state(records: list, now: datetime, tz: Optional[tzinfo] = None) -> State:
    state = State(now=now, tz=tz)
    _load(state, records)
    present = [g for g in GROUPS if state.grouped[g]]
    # Older starts collapsed, unless it is all there is.
    state.open_groups = {g for g in present if g != OLDER} or set(present)
    state.cursor = _first_tab(rows(state))
    return state


def rows(state: State) -> list:
    out = []
    for name in GROUPS:
        members = state.grouped.get(name, [])
        if state.filter_text:
            members = [r for r in members if _matches(r, state.filter_text)]
        if not members:
            continue
        is_open = name in state.open_groups
        out.append(Row("header", name, None, len(members), is_open))
        if is_open:
            out.extend(Row("tab", name, r) for r in members)
    return out


def _first_tab(rs: list) -> int:
    return next((i for i, r in enumerate(rs) if r.kind == "tab"), 0)


def _too_small(state: State) -> bool:
    return state.height < 5 or state.width < 30


def layout(height: int):
    """(list rows, show the scroll markers, show the details line) for a popup `height` rows tall."""
    if height >= LIST_ROWS + 5:
        return LIST_ROWS, True, True
    if height >= LIST_ROWS + 4:
        return LIST_ROWS, True, False
    return max(min(LIST_ROWS, height - 2), 1), False, False


def window_row(state: State, y: int):
    """The list-window row at screen line `y`, or None when `y` is outside the list."""
    if _too_small(state):  # render shows only TOO_SMALL: there is no list to click
        return None
    win, markers, _ = layout(state.height)
    row = y - (2 if markers else 1)
    return row if 0 <= row < win else None


def _follow(state: State) -> None:
    """Clamp the cursor, and scroll so it stays inside the list window."""
    n = len(rows(state))
    win = layout(state.height)[0]
    state.cursor = max(0, min(state.cursor, n - 1))
    if state.cursor < state.offset:
        state.offset = state.cursor
    elif state.cursor >= state.offset + win:
        state.offset = state.cursor - win + 1
    state.offset = max(0, min(state.offset, max(n - win, 0)))


def _current(state: State):
    rs = rows(state)
    return rs[state.cursor] if 0 <= state.cursor < len(rs) else None


def _toggle(state: State, group: str) -> None:
    state.open_groups ^= {group}
    _follow(state)


def _set_filter(state: State, text: str) -> None:
    if text and state.saved_open is None:
        state.saved_open = set(state.open_groups)
    state.filter_text = text
    if text:
        state.open_groups = {g for g in GROUPS if any(_matches(r, text) for r in state.grouped[g])}
    else:
        if state.saved_open is not None:
            state.open_groups = state.saved_open
        state.saved_open = None
    state.cursor = _first_tab(rows(state))
    state.offset = 0
    _follow(state)


def _row_key(row: Row):
    return ("tab", row.record["id"]) if row.kind == "tab" else ("header", row.group)


def refresh(state: State, records: list) -> None:
    """Swap in a re-read archive, keeping the cursor on the same row when it still exists.

    Groups follow the start-up rules: a group that gains its first entries
    opens (Older only if it is the only group), and while filtering every
    group with a match is shown open.
    """
    cur = _current(state)
    key = _row_key(cur) if cur else None
    old = state.cursor
    before = {g for g in GROUPS if state.grouped.get(g)}
    _load(state, records)
    present = [g for g in GROUPS if state.grouped[g]]
    appeared = {g for g in present if g not in before and g != OLDER}
    if present == [OLDER]:
        appeared.add(OLDER)
    state.open_groups |= appeared
    if state.saved_open is not None:
        state.saved_open |= appeared
    if state.filter_text:
        state.open_groups |= {g for g in present if any(_matches(r, state.filter_text) for r in state.grouped[g])}
    keys = [_row_key(r) for r in rows(state)]
    state.cursor = keys.index(key) if key in keys else old
    _follow(state)


def reduce(state: State, event):
    """Apply one input event to `state` (in place); return an action or None.

    Events: "up", "down", "pgup", "pgdn", "home", "end", "enter", "left",
    "right", "backspace", "esc", "wheelup", "wheeldown", "other" (any
    other key: it cancels a delete confirmation), ("char", c),
    ("click", window_row), ("dclick", window_row), ("resize", h, w).
    Actions: ("restore", id), ("delete", id), ("quit",).
    """
    kind = event[0] if isinstance(event, tuple) else event
    if kind == "resize":
        state.height, state.width = event[1], event[2]
        _follow(state)
        return None
    state.message = ""  # a message lasts until the next key press
    if state.total == 0:
        return ("quit",)
    if _too_small(state):  # nothing is on screen to act on: only closing works
        return ("quit",) if kind == "esc" or event == ("char", "q") else None
    if state.mode == "confirm":
        state.mode = "move"
        cur = _current(state)
        if kind == "char" and event[1] in ("y", "Y") and cur and cur.kind == "tab":
            return ("delete", cur.record["id"])
        return None
    if state.mode == "filter":
        if kind == "char":
            _set_filter(state, state.filter_text + event[1])
        elif kind == "backspace":
            if state.filter_text:
                _set_filter(state, state.filter_text[:-1])
            else:
                state.mode = "move"
        elif kind == "enter":
            state.mode = "move"
        elif kind == "esc":
            if state.filter_text:  # backing out of an empty "/" must not move the cursor
                _set_filter(state, "")
            state.mode = "move"
        elif kind in ("click", "dclick", "wheelup", "wheeldown"):
            state.mode = "move"  # keep the filter, act on what was clicked
            return _reduce_move(state, kind, event)
        return None
    return _reduce_move(state, kind, event)


def _reduce_move(state: State, kind: str, event):
    if kind == "char":
        c = event[1]
        kind = _VIM_KEYS.get(c, c)
    rs = rows(state)
    if kind in _STEPS or kind in ("home", "end"):
        if kind == "home":
            state.cursor = 0
        elif kind == "end":
            state.cursor = len(rs) - 1
        else:
            state.cursor += _STEPS[kind]
        _follow(state)
        return None
    if kind in ("click", "dclick"):
        index = state.offset + event[1]
        if index >= len(rs):
            return None
        state.cursor = index
        row = rs[index]
        if row.kind == "header":
            _toggle(state, row.group)
            return None
        return ("restore", row.record["id"]) if kind == "dclick" else None
    cur = _current(state)
    if kind == "q":
        return ("quit",)
    if kind == "esc":
        if state.filter_text:
            _set_filter(state, "")
            return None
        return ("quit",)
    if kind == "/":
        state.mode = "filter"
        return None
    if cur is None:
        return None
    if kind == "enter":
        if cur.kind == "tab":
            return ("restore", cur.record["id"])
        _toggle(state, cur.group)
    elif kind == "right":
        if cur.group not in state.open_groups:
            _toggle(state, cur.group)
    elif kind == "left":
        if cur.kind == "tab":
            state.cursor = next(i for i, r in enumerate(rs) if r.kind == "header" and r.group == cur.group)
        if cur.group in state.open_groups:
            _toggle(state, cur.group)
    elif kind == "d" and cur.kind == "tab":
        state.mode = "confirm"
    return None


def _tab_line(record: dict, selected: bool, width: int, now: datetime) -> str:
    limit = max(width - 1, 20)
    fixed = 3 + 1 + 12 + 1 + 4  # " > " prefix, gaps, agent column, idle column
    room = max(limit - fixed, 10)
    tab_w = max(int(room * 0.6), 8)
    ws_w = max(room - tab_w - 1, 6)
    tab = (record.get("tab", {}).get("label") or "(unnamed)")[:tab_w]
    workspace = (record.get("workspace", {}).get("label") or "")[:ws_w]
    names = agent_names(record)[:12]
    days = days_idle(record, now)
    idle = "?" if days is None else f"{days}d"
    return f" {'>' if selected else ' '} {tab:<{tab_w}} {workspace:<{ws_w}} {names:<12} {idle}".rstrip()


def _details(record: dict, tz: Optional[tzinfo]) -> str:
    cwd = record.get("workspace", {}).get("cwd") or ""
    home = str(Path.home())
    if cwd == home or cwd.startswith(home + os.sep):
        cwd = "~" + cwd[len(home):]
    at = parse_iso(record.get("archived_at"))
    shelved = ""
    if at is not None:
        local = at.astimezone(tz)
        shelved = f"shelved {local:%b} {local.day} {local:%H:%M}"
    panes = record.get("panes", {})
    names = agent_names(record)
    count = f"{len(panes)} pane{'' if len(panes) == 1 else 's'}" + (f": {names}" if names else "")
    return " " + "  ".join(p for p in (cwd, shelved, count) if p)


def _status(state: State) -> str:
    if state.mode == "confirm":
        cur = _current(state)
        return f' Delete "{record_label(cur.record)}"? This cannot be undone. [y/N]'
    if state.mode == "filter":
        return f" /{state.filter_text}"
    if state.message:
        return " " + state.message
    if state.filter_text:
        return f" /{state.filter_text}  (Esc clears the filter)"
    return KEYS_HINT


def render(state: State):
    """The lines to draw, and the index of the highlighted line (or None)."""
    if _too_small(state):
        return [TOO_SMALL], None
    limit = state.width - 1  # never write the last column
    if state.total == 0:
        return ["No archived tabs.", "Press any key to close."], None
    rs = rows(state)
    win, markers, details = layout(state.height)
    lines = [f" Shelf - {state.total} archived"]
    above, below = state.offset, max(len(rs) - state.offset - win, 0)
    if markers:
        lines.append(f" ^ {above} more" if above else "")
    highlight = None
    listed = []
    for index, row in enumerate(rs[state.offset:state.offset + win], state.offset):
        if index == state.cursor:
            highlight = len(lines) + len(listed)
        if row.kind == "header":
            listed.append(f" {'-' if row.is_open else '+'} {row.group} ({row.count})")
        else:
            listed.append(_tab_line(row.record, index == state.cursor, state.width, state.now))
    if not rs:
        listed.append(" No match")
    lines.extend(listed + [""] * (win - len(listed)))
    if markers:
        lines.append(f" v {below} more" if below else "")
    if details:
        cur = _current(state)
        lines.append(_details(cur.record, state.tz) if cur and cur.kind == "tab" else "")
    lines.append(_status(state))
    return [line[:limit] for line in lines], highlight


def _ignore_sigint():
    """Best-effort: signal handling can only be changed from the main thread."""
    try:
        return signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        return None


def _restore_sigint(previous) -> None:
    if previous is None:
        return
    try:
        signal.signal(signal.SIGINT, previous)
    except (ValueError, OSError):
        pass


def apply(state: State, action, arch, do_restore, notify, draw=lambda: None) -> bool:
    """Carry out a reducer action. Returns True when the popup should close."""
    if action is None:
        return False
    if action[0] == "quit":
        return True
    archive_id = action[1]
    if action[0] == "delete":
        state.message = "Waiting for a sweep to finish..."
        draw()
        state.message = ""
        try:
            # Under the same lock a sweep uses, so a delete never races a
            # sweep archiving or reading the same archive directory.
            with FileLock(Path(arch.root).parent / "sweep.lock", wait_seconds=DELETE_LOCK_WAIT_SECONDS):
                arch.delete(archive_id)
        except LockBusy:
            state.message = SWEEP_BUSY
        refresh(state, arch.list())
        return False
    cur = _current(state)
    state.message = f"Restoring {record_label(cur.record) if cur and cur.record else archive_id}..."
    draw()
    previous = _ignore_sigint()
    try:
        result = do_restore(archive_id)
    except LockBusy:
        state.message = SWEEP_BUSY
    except Exception as e:  # keep the entry and the popup; show why
        state.message = f"Restore failed: {e}"
    else:
        warnings = result.get("warnings") or []
        if warnings:
            notify("shelf", "; ".join(warnings))
        return True
    finally:
        _restore_sigint(previous)
    refresh(state, arch.list())
    return False


def run(arch, do_restore, now_fn, notify=lambda title, body: None) -> None:
    # Before curses starts: ncurses reads ESCDELAY once, and its default of
    # 1000 ms makes a single Esc feel stuck.
    os.environ.setdefault("ESCDELAY", "25")
    import curses

    curses.wrapper(_loop, arch, do_restore, now_fn, notify)


def _loop(screen, arch, do_restore, now_fn, notify) -> None:
    import curses

    try:
        curses.curs_set(0)
    except curses.error:
        pass  # a terminal that cannot hide the cursor
    try:
        curses.use_default_colors()  # keep the terminal's own colours, not white on black
    except curses.error:
        pass  # a terminal without colours
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    state = initial_state(arch.list(), now_fn().astimezone())
    reduce(state, ("resize",) + screen.getmaxyx())

    def draw():
        _draw(curses, screen, state)

    while True:
        draw()
        event = _read_event(curses, screen, state)
        if event is not None and apply(state, reduce(state, event), arch, do_restore, notify, draw):
            return


def _draw(curses, screen, state: State) -> None:
    screen.erase()
    lines, highlight = render(state)
    for y, line in enumerate(lines[:state.height]):
        attr = curses.A_REVERSE if y == highlight else curses.A_NORMAL
        try:
            # insstr, not addstr: it stops at the right margin instead of
            # wrapping, so wide characters never spill into the next row.
            screen.insstr(y, 0, line.ljust(state.width - 1) if y == highlight else line, attr)
        except curses.error:
            pass
    screen.refresh()


def _read_event(curses, screen, state: State):
    key = screen.get_wch()
    if isinstance(key, str):
        if key in ("\n", "\r"):
            return "enter"
        if key in ("\x7f", "\b"):
            return "backspace"
        if key == "\x1b":
            return _after_escape(curses, screen)
        return ("char", key) if key.isprintable() else "other"
    if key == curses.KEY_RESIZE:
        return ("resize",) + screen.getmaxyx()
    if key == curses.KEY_MOUSE:
        return _mouse_event(curses, state)
    return {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right",
            curses.KEY_PPAGE: "pgup", curses.KEY_NPAGE: "pgdn", curses.KEY_HOME: "home", curses.KEY_END: "end",
            curses.KEY_ENTER: "enter", curses.KEY_BACKSPACE: "backspace"}.get(key, "other")


def _after_escape(curses, screen):
    """Tell a lone Esc from an arrow key curses did not decode (ESC [ B or ESC O B)."""
    screen.timeout(50)
    try:
        nxt = _poll(curses, screen)
        if nxt is None:
            return "esc"
        if nxt not in ("[", "O"):
            return "other"  # Alt+key: ignore it rather than close the popup
        final = _poll(curses, screen)
        # Skip a CSI's parameter bytes (ESC [ 1 ; 5 A), so they never arrive as typed text.
        while nxt == "[" and isinstance(final, str) and " " <= final <= "?":
            final = _poll(curses, screen)
        return {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}.get(final, "other")
    finally:
        screen.timeout(-1)


def _poll(curses, screen):
    try:
        return screen.get_wch()
    except curses.error:
        return None


def _mouse_event(curses, state: State):
    try:
        _, _, y, _, buttons = curses.getmouse()
    except curses.error:
        return None
    # Python exports BUTTON5_* only from 3.10. On ncurses' mouse version 2
    # (BUTTON4_PRESSED == 2 << 15) wheel-down is 2 << 20; on version 1 there is none.
    wheel_down = getattr(curses, "BUTTON5_PRESSED", 2 << 20 if curses.BUTTON4_PRESSED == 2 << 15 else 0)
    if buttons & curses.BUTTON4_PRESSED:
        return "wheelup"
    if wheel_down and buttons & wheel_down:
        return "wheeldown"
    row = window_row(state, y)
    if row is None:
        return None
    if buttons & curses.BUTTON1_DOUBLE_CLICKED:
        return ("dclick", row)
    if buttons & (curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED):  # a slow click arrives as a release
        return ("click", row)
    return None
