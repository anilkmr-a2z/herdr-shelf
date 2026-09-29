# Picker Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the numbered `input()` restore picker with a curses popup. It
has arrow keys, archive-date groups (with "Older" collapsed), a 10-row
scrolling window, a details line, a `/` filter and mouse support.

**Architecture:** `shelf/picker.py` holds pure logic with no terminal:
- a `State` dataclass;
- `reduce(state, event)`, which updates the state and returns an action;
- `render(state)`, which returns the lines to draw and the highlighted index;
- `apply(state, action, ...)`, which does the restore or delete.

It is all unit-tested. A thin `curses.wrapper` loop, `run()`, reads keys and
mouse input, turns them into events, and draws. The design is in
`docs/superpowers/specs/2026-09-29-picker-redesign-design.md`; read it first.

**Tech Stack:** Python 3.9+ standard library only (`curses`, `dataclasses`,
`pty` for one test), with `unittest`.

---

## Before you start

- Work on a branch: `git checkout -b picker-redesign` from `main`.
- **Test commands.** The full suite is
  `python3 -m unittest discover -s tests -t . -v`. CI runs it on Linux and
  macOS under Python 3.9 and 3.12, so run it under both when you can:
  `python3.9 -m ...` and `python3.12 -m ...`.
- **Dependencies.** Standard library only. Don't add a dependency.
- **Commits.** Stage files by explicit path, never `git add -A` or
  `git add .`. The message is `type: summary`, then a body, then an
  `Assisted by AI` line. The author name and email are already set in the
  repo config.
- A local post-commit history check may warn "more than one feature commit".
  That is expected until Task 7 squashes the branch into one commit.
- Don't push.

## File structure

| File | Change | Task |
|---|---|---|
| `shelf/picker.py` | Rewritten step by step. `days_idle`, `record_label`, `agent_names`, `DELETE_LOCK_WAIT_SECONDS`, `_ignore_sigint` and `_restore_sigint` stay. | 1-5 |
| `tests/test_picker.py` | New tests added per task; the whole file is replaced in Task 5 | 1-5 |
| `tests/test_picker_tty.py` | New: drives the curses loop in a pseudo-terminal | 5 |
| `shelf/__main__.py` | `_pick`: new `run()` call; the stderr log handler is off while the popup is up | 5 |
| `tests/test_main.py` | Two `pick` tests rewritten | 5 |
| `herdr-plugin.toml`, `tests/test_manifest.py` | Popup height 18 (Task 5); version 0.4.0 (Task 6) | 5, 6 |
| `README.md`, `AGENTS.md`, `CHANGELOG.md`, `shelf/__init__.py`, `docs/superpowers/specs/2026-09-24-herdr-shelf-design.md` | Docs and version | 6 |

## Concepts you need

- **An archive record** is a dict:
  `{"id", "archived_at": "2026-09-25T21:41:17Z", "tab": {"label"},
  "workspace": {"label", "cwd"}, "panes": {<id>: {"agent", "last_activity"}}}`.
  `Archive.list()` returns them; `Archive.delete(id)` removes one.
- **Groups** come from `archived_at`, counted in the machine's local calendar
  days: 0 is "Archived today", 1-6 is "Last 7 days", 7-29 is "Last 30 days",
  and 30 or more, or a missing timestamp, is "Older". Every function that
  takes `now` expects an aware local datetime. The loop passes
  `now_fn().astimezone()`, so tests pick a fixed time zone and never touch
  `TZ`.
- **Rows** are what the list shows: `Row("header", group, None, count,
  is_open)` or `Row("tab", group, record)`. `state.cursor` indexes
  `rows(state)`. Row indexes shift when a group opens, closes or is filtered,
  so anything that must survive a re-read (such as `refresh`) keys on the
  record id.
- **Events** are strings (`"up"`, `"enter"`, ...) or tuples (`("char", c)`,
  `("click", row)`, `("resize", h, w)`). **Actions** are `("restore", id)`,
  `("delete", id)` and `("quit",)`.
- **herdr popups get every key**, Esc included, and close only when the
  command exits. So the picker must exit on q, on Esc, and after a successful
  restore.

---

### Task 1: Groups, sorting and the initial state

**Files:**
- Modify: `shelf/picker.py` (imports; append new code at the end)
- Test: `tests/test_picker.py` (imports; new fixtures; two new test classes)

- [ ] **Step 1: Update the test imports and add the fixtures**

In `tests/test_picker.py`, change the two import lines:

```python
from datetime import datetime, timedelta, timezone
```

```python
from shelf.util import FileLock, LockBusy, iso
```

Then insert this block right after the existing `RECORDS = [...]` list and
before `class FakeArchive:`. Leave the old test classes alone; Task 5
replaces them.

```python
LOCAL = timezone(timedelta(hours=-7))  # the "local" zone for every test here
NOW = datetime(2026, 9, 29, 10, 0, tzinfo=LOCAL)  # 17:00Z
TODAY_AT = "2026-09-29T16:14:00Z"  # 09:14 local
WEEK_AT = "2026-09-25T21:41:17Z"
MONTH_AT = "2026-09-10T12:00:00Z"
OLDER_AT = "2026-08-01T12:00:00Z"


def rec(archive_id, archived_at, label, workspace="backend", agent="claude", idle=20, cwd=None):
    last = iso(NOW - timedelta(days=idle, hours=1))
    return {"id": archive_id, "archived_at": archived_at, "tab": {"label": label},
            "workspace": {"label": workspace, "cwd": cwd},
            "panes": {"p": {"agent": agent, "last_activity": last}}}


HOME_SRC = str(Path.home() / "src" / "backend")
ELEVEN = [
    rec("t1", TODAY_AT, "perf-probe", idle=21),
    rec("t2", TODAY_AT, "flaky-test", agent="codex", idle=15, cwd=HOME_SRC),
    rec("w1", WEEK_AT, "onboarding", workspace="docs", idle=28),
    rec("w2", WEEK_AT, "api-refactor", idle=17),
    rec("w3", WEEK_AT, "release-notes", workspace="docs", idle=18),
    rec("m1", MONTH_AT, "db-migration", idle=34),
    rec("m2", "2026-09-09T12:00:00Z", "style-guide", workspace="docs", idle=42),
    rec("o1", OLDER_AT, "lint-cleanup", idle=43),
    rec("o2", OLDER_AT, "api-sketch", idle=60),
    rec("o3", OLDER_AT, "old-spike", idle=32),
    rec("o4", "2026-07-01T12:00:00Z", "misc", idle=90),
]


def state_of(records=ELEVEN, height=16, width=80):
    state = picker.initial_state(records, NOW)
    state.height, state.width = height, width
    return state


def press(state, *events):
    action = None
    for event in events:
        action = picker.reduce(state, ("char", event) if isinstance(event, str) and len(event) == 1 else event)
    return action


def current(state):
    return picker.rows(state)[state.cursor]


def labels(state):
    return [r.group if r.kind == "header" else r.record["tab"]["label"] for r in picker.rows(state)]
```

Note that `state_of` sets the popup size directly, so these fixtures work
before `reduce` exists.

- [ ] **Step 2: Write the failing tests**

Add these two classes just above the `if __name__ == "__main__":` line:

```python
class GroupTest(unittest.TestCase):
    def group(self, archived_at):
        return picker.group_of({"archived_at": archived_at}, NOW)

    def test_boundaries_in_local_calendar_days(self):
        cases = {
            "2026-09-29T07:00:00Z": picker.TODAY,  # 00:00 local today
            "2026-09-29T06:59:00Z": picker.WEEK,  # 23:59 local yesterday
            "2026-09-23T12:00:00Z": picker.WEEK,  # 6 days ago
            "2026-09-22T12:00:00Z": picker.MONTH,  # 7 days ago
            "2026-08-31T12:00:00Z": picker.MONTH,  # 29 days ago
            "2026-08-30T12:00:00Z": picker.OLDER,  # 30 days ago
            "2026-10-01T12:00:00Z": picker.TODAY,  # a clock that ran ahead
        }
        for archived_at, expected in cases.items():
            with self.subTest(archived_at=archived_at):
                self.assertEqual(self.group(archived_at), expected)

    def test_a_utc_timestamp_on_a_different_local_date(self):
        # 03:00Z on the 29th is 20:00 local on the 28th: yesterday, not today.
        self.assertEqual(self.group("2026-09-29T03:00:00Z"), picker.WEEK)

    def test_missing_or_unparseable_archived_at_is_older(self):
        for value in (None, "", "not-a-date"):
            with self.subTest(value=value):
                self.assertEqual(self.group(value), picker.OLDER)


class InitialStateTest(unittest.TestCase):
    def test_older_starts_collapsed_and_the_rest_open(self):
        self.assertEqual(labels(state_of()), [
            picker.TODAY, "flaky-test", "perf-probe",
            picker.WEEK, "api-refactor", "release-notes", "onboarding",
            picker.MONTH, "db-migration", "style-guide",
            picker.OLDER])

    def test_sorted_newest_archive_first_then_least_idle_first(self):
        # Within Today both came from one sweep, so the less idle one leads;
        # within Last 30 days the more recently archived one leads.
        rs = picker.rows(state_of())
        self.assertEqual([r.record["id"] for r in rs if r.kind == "tab"][:2], ["t2", "t1"])
        self.assertEqual([r.record["id"] for r in rs if r.group == picker.MONTH and r.kind == "tab"],
                         ["m1", "m2"])

    def test_older_opens_when_it_is_the_only_group(self):
        state = state_of([r for r in ELEVEN if r["id"].startswith("o")])
        self.assertEqual(labels(state), [picker.OLDER, "old-spike", "lint-cleanup", "api-sketch", "misc"])

    def test_empty_groups_are_not_shown(self):
        state = state_of([r for r in ELEVEN if r["id"] in ("t1", "o1")])
        self.assertEqual(labels(state), [picker.TODAY, "perf-probe", picker.OLDER])

    def test_cursor_starts_on_the_first_tab_row(self):
        self.assertEqual(current(state_of()).record["id"], "t2")

    def test_no_activity_timestamp_still_lists_the_entry(self):
        entry = dict(rec("x", TODAY_AT, "blank"), panes={})
        state = state_of([entry])
        self.assertEqual(labels(state), [picker.TODAY, "blank"])
```

- [ ] **Step 3: Run them to make sure they fail**

Run: `python3 -m unittest tests.test_picker -v`
Expected: 11 errors, such as `AttributeError: module 'shelf.picker' has no attribute 'group_of'`.

- [ ] **Step 4: Implement**

In `shelf/picker.py`, replace the lines `from datetime import datetime` and
`from pathlib import Path` with:

```python
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, Optional
```

Then append this at the end of the file:

```python
LIST_ROWS = 10
TODAY, WEEK, MONTH, OLDER = "Archived today", "Last 7 days", "Last 30 days", "Older"
GROUPS = (TODAY, WEEK, MONTH, OLDER)


def group_of(record: dict, now: datetime) -> str:
    """The record's group, by local calendar days since archived_at. `now` is aware local time."""
    at = parse_iso(record.get("archived_at"))
    if at is None:
        return OLDER
    days = (now.date() - at.astimezone(now.tzinfo).date()).days
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
        state.grouped[group_of(rec, state.now)].append(rec)


def initial_state(records: list, now: datetime) -> State:
    state = State(now=now)
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
```

- [ ] **Step 5: Run the tests to make sure they pass**

Run: `python3 -m unittest discover -s tests -t .`
Expected: `OK` (395 tests).

- [ ] **Step 6: Commit**

```bash
git add shelf/picker.py tests/test_picker.py
git commit -m "feat: group archived tabs by archive date for the new picker" -m "Assisted by AI"
```

---

### Task 2: Moving, groups, scrolling and the mouse

**Files:**
- Modify: `shelf/picker.py` (append)
- Test: `tests/test_picker.py` (two new classes)

- [ ] **Step 1: Write the failing tests**

Add these above `if __name__ == "__main__":`:

```python
class MoveTest(unittest.TestCase):
    def test_up_and_down_stop_at_the_ends(self):
        state = state_of()
        press(state, "up", "up", "up")
        self.assertEqual(state.cursor, 0)
        press(state, *["down"] * 20)
        self.assertEqual(current(state).group, picker.OLDER)

    def test_vim_keys_move_like_the_arrows(self):
        state = state_of()
        press(state, "j")
        self.assertEqual(current(state).record["id"], "t1")
        press(state, "k")
        self.assertEqual(current(state).record["id"], "t2")

    def test_page_home_and_end(self):
        state = state_of()
        press(state, "pgdn")
        self.assertEqual(state.cursor, 10)
        press(state, "pgup")
        self.assertEqual(state.cursor, 0)
        press(state, "end")
        self.assertEqual(state.cursor, len(picker.rows(state)) - 1)
        press(state, "home")
        self.assertEqual(state.cursor, 0)

    def test_enter_on_a_tab_restores_it(self):
        self.assertEqual(press(state_of(), "enter"), ("restore", "t2"))

    def test_enter_on_a_header_toggles_the_group(self):
        state = state_of()
        press(state, "end", "enter")
        self.assertIn("old-spike", labels(state))
        press(state, "enter")
        self.assertNotIn("old-spike", labels(state))

    def test_right_opens_and_left_closes(self):
        state = state_of()
        press(state, "end", "right")
        self.assertIn("old-spike", labels(state))
        press(state, "right")  # already open: stays open
        self.assertIn("old-spike", labels(state))
        press(state, "left")
        self.assertNotIn("old-spike", labels(state))

    def test_left_on_a_tab_jumps_to_its_header_and_closes_it(self):
        state = state_of()
        press(state, "down", "down", "down")  # api-refactor, under Last 7 days
        press(state, "h")
        self.assertEqual(current(state), picker.Row("header", picker.WEEK, None, 3, False))
        self.assertNotIn("api-refactor", labels(state))

    def test_q_and_esc_quit(self):
        self.assertEqual(press(state_of(), "q"), ("quit",))
        self.assertEqual(press(state_of(), "esc"), ("quit",))

    def test_scrolling_keeps_the_cursor_in_the_ten_row_window(self):
        state = state_of()
        press(state, "end", "right")  # all 15 rows showing
        press(state, "end")
        self.assertEqual((state.cursor, state.offset), (14, 5))
        press(state, *["up"] * 9)
        self.assertEqual((state.cursor, state.offset), (5, 5))
        press(state, "up")
        self.assertEqual((state.cursor, state.offset), (4, 4))

    def test_wheel_moves_three_rows(self):
        state = state_of()
        press(state, "wheeldown")
        self.assertEqual(state.cursor, 4)
        press(state, "wheelup")
        self.assertEqual(state.cursor, 1)

    def test_click_highlights_and_double_click_restores(self):
        state = state_of()
        self.assertIsNone(press(state, ("click", 5)))
        self.assertEqual(current(state).record["id"], "w3")
        self.assertEqual(press(state, ("dclick", 5)), ("restore", "w3"))

    def test_click_on_a_header_toggles_it(self):
        state = state_of()
        press(state, ("click", 0))
        self.assertEqual(current(state).group, picker.TODAY)
        self.assertEqual(current(state).kind, "header")
        self.assertNotIn("flaky-test", labels(state))

    def test_click_accounts_for_the_scroll_offset_and_ignores_empty_rows(self):
        state = state_of()
        press(state, "end", "right", "end")  # offset 5
        press(state, ("click", 0))
        self.assertEqual(current(state).record["id"], "w3")
        state = state_of()
        self.assertIsNone(press(state, ("click", 9)))  # rows 0-10 exist; row 9 is style-guide
        self.assertEqual(current(state).record["id"], "m2")
        self.assertIsNone(press(state, ("click", 11)))
        self.assertEqual(current(state).record["id"], "m2")


class WindowRowTest(unittest.TestCase):
    def test_maps_screen_lines_to_list_rows(self):
        state = state_of(height=16)
        self.assertEqual([picker.window_row(state, y) for y in (1, 2, 11, 12)], [None, 0, 9, None])
        state = state_of(height=13)  # no markers: the list starts right under the title
        self.assertEqual(picker.window_row(state, 1), 0)
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python3 -m unittest tests.test_picker -v`
Expected: 14 errors, `AttributeError: module 'shelf.picker' has no attribute 'reduce'`.

- [ ] **Step 3: Implement**

Append this to `shelf/picker.py`. `reduce` and `_reduce_move` are
deliberately incomplete here: Task 3 replaces both with versions that also
handle the filter and delete confirmation.

```python
_VIM_KEYS = {"k": "up", "j": "down", "h": "left", "l": "right"}
_STEPS = {"up": -1, "down": 1, "pgup": -LIST_ROWS, "pgdn": LIST_ROWS, "wheelup": -3, "wheeldown": 3}


def layout(height: int):
    """(list rows, show the scroll markers, show the details line) for a popup `height` rows tall."""
    if height >= LIST_ROWS + 5:
        return LIST_ROWS, True, True
    if height >= LIST_ROWS + 4:
        return LIST_ROWS, True, False
    return max(min(LIST_ROWS, height - 2), 1), False, False


def window_row(state: State, y: int):
    """The list-window row at screen line `y`, or None when `y` is outside the list."""
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


def reduce(state: State, event):
    """Apply one input event to `state` (in place); return an action or None.

    Events: "up", "down", "pgup", "pgdn", "home", "end", "enter", "left",
    "right", "backspace", "esc", "wheelup", "wheeldown", ("char", c),
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
        return ("quit",)
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
    return None
```

Some notes:
- `_follow` is the only place the cursor is clamped and the window
  scrolled. Call it after anything that moves the cursor or changes the rows.
- `layout()` decides how many list rows fit. `window_row()` maps a screen
  line to a list-window row for mouse clicks. The title is line 0, and the up
  marker is line 1 when markers are shown.

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `python3 -m unittest discover -s tests -t .`
Expected: `OK` (409 tests).

- [ ] **Step 5: Commit**

```bash
git add shelf/picker.py tests/test_picker.py
git commit -m "feat: arrow-key movement, groups and scrolling for the new picker" -m "Assisted by AI"
```

---

### Task 3: Filter and delete confirmation

**Files:**
- Modify: `shelf/picker.py` (replace `reduce` and `_reduce_move`; append `_set_filter`)
- Test: `tests/test_picker.py` (two new classes)

- [ ] **Step 1: Write the failing tests**

Add these above `if __name__ == "__main__":`:

```python
class FilterTest(unittest.TestCase):
    def test_typing_narrows_and_opens_groups_with_a_match(self):
        state = state_of()
        press(state, "/", "a", "p", "i")
        self.assertEqual(state.mode, "filter")
        self.assertEqual(labels(state), [picker.WEEK, "api-refactor", picker.OLDER, "api-sketch"])
        self.assertEqual(current(state).record["id"], "w2")

    def test_matches_workspace_and_agent_names_case_insensitively(self):
        state = state_of()
        press(state, "/", "C", "O", "D", "E", "X")
        self.assertEqual(labels(state), [picker.TODAY, "flaky-test"])
        press(state, "esc", "/", "d", "o", "c", "s")
        self.assertEqual(set(labels(state)) - set(picker.GROUPS),
                         {"onboarding", "release-notes", "style-guide"})

    def test_filter_keys_are_typed_as_text(self):
        state = state_of()
        self.assertIsNone(press(state, "/", "q", "d", "j", "/"))
        self.assertEqual((state.mode, state.filter_text), ("filter", "qdj/"))

    def test_no_match(self):
        state = state_of()
        press(state, "/", "z", "z", "z")
        self.assertEqual(picker.rows(state), [])

    def test_enter_keeps_the_filter_then_esc_clears_it_then_esc_quits(self):
        state = state_of()
        press(state, "/", "a", "p", "i", "enter")
        self.assertEqual((state.mode, state.filter_text), ("move", "api"))
        self.assertEqual(press(state, "down"), None)
        self.assertEqual(current(state).group, picker.OLDER)
        self.assertIsNone(press(state, "esc"))
        self.assertEqual(state.filter_text, "")
        self.assertEqual(press(state, "esc"), ("quit",))

    def test_backspace_deletes_then_leaves_filter_mode(self):
        state = state_of()
        press(state, "/", "a", "backspace")
        self.assertEqual((state.mode, state.filter_text), ("filter", ""))
        press(state, "backspace")
        self.assertEqual(state.mode, "move")

    def test_clearing_the_filter_restores_the_open_groups(self):
        state = state_of()
        before = labels(state)
        press(state, "/", "a", "p", "i")  # opens Older
        press(state, "esc")
        self.assertEqual((state.mode, labels(state)), ("move", before))


class ConfirmTest(unittest.TestCase):
    def test_d_then_y_deletes(self):
        state = state_of()
        self.assertIsNone(press(state, "d"))
        self.assertEqual(state.mode, "confirm")
        self.assertEqual(press(state, "y"), ("delete", "t2"))
        self.assertEqual(state.mode, "move")

    def test_anything_else_cancels(self):
        for key in ("n", "N", "esc", "enter", "q", "down"):
            with self.subTest(key=key):
                state = state_of()
                press(state, "d")
                self.assertIsNone(press(state, key))
                self.assertEqual((state.mode, current(state).record["id"]), ("move", "t2"))

    def test_d_on_a_header_does_nothing(self):
        state = state_of()
        press(state, "home", "d")
        self.assertEqual(state.mode, "move")
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python3 -m unittest tests.test_picker -v`
Expected: 11 failures, such as `AssertionError: ('quit',) is not None`. In
Task 2, `d` does nothing, so Esc quits and Enter restores.

- [ ] **Step 3: Implement**

In `shelf/picker.py`, replace the whole `reduce` and `_reduce_move`
functions from Task 2 with:

```python
def reduce(state: State, event):
    """Apply one input event to `state` (in place); return an action or None.

    Events: "up", "down", "pgup", "pgdn", "home", "end", "enter", "left",
    "right", "backspace", "esc", "wheelup", "wheeldown", ("char", c),
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
            _set_filter(state, "")
            state.mode = "move"
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
```

Then append:

```python
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
```

How the filter works:
- The first keystroke saves `open_groups` into `saved_open`.
- While the filter is set, `open_groups` is exactly the groups that contain
  a match, and `rows()` hides the others.
- Clearing the filter puts `saved_open` back.
- Toggling a group while filtered still works, because toggles act on
  `open_groups`.

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `python3 -m unittest discover -s tests -t .`
Expected: `OK` (419 tests).

- [ ] **Step 5: Commit**

```bash
git add shelf/picker.py tests/test_picker.py
git commit -m "feat: filter and delete confirmation for the new picker" -m "Assisted by AI"
```

---

### Task 4: Refresh, restore and delete

**Files:**
- Modify: `shelf/picker.py` (append)
- Test: `tests/test_picker.py` (two new classes)

- [ ] **Step 1: Write the failing tests**

Add these above `if __name__ == "__main__":`:

```python
class RefreshTest(unittest.TestCase):
    def test_keeps_the_cursor_on_the_same_entry(self):
        state = state_of()
        press(state, "down", "down", "down")  # api-refactor
        picker.refresh(state, [r for r in ELEVEN if r["id"] not in ("t1", "t2")])
        self.assertEqual(current(state).record["id"], "w2")

    def test_falls_back_to_the_same_position_when_the_entry_is_gone(self):
        state = state_of()
        press(state, "down")  # perf-probe, row 2
        picker.refresh(state, [r for r in ELEVEN if r["id"] != "t1"])
        self.assertEqual(state.cursor, 2)
        self.assertEqual(current(state).kind, "header")


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.arch = FakeArchive(ELEVEN)
        self.state = state_of(self.arch.list())
        self.restored, self.notifications = [], []

    def apply(self, action, restore_error=None, warnings=()):
        def do_restore(archive_id):
            self.restored.append(archive_id)
            if restore_error:
                raise restore_error
            return {"tab_id": "t", "warnings": list(warnings)}

        return picker.apply(self.state, action, self.arch, do_restore,
                            lambda title, body: self.notifications.append((title, body)))

    def test_quit_and_no_action(self):
        self.assertTrue(self.apply(("quit",)))
        self.assertFalse(self.apply(None))

    def test_restore_closes_the_popup(self):
        self.assertTrue(self.apply(("restore", "t2")))
        self.assertEqual((self.restored, self.notifications), (["t2"], []))

    def test_restore_warnings_are_notified(self):
        self.assertTrue(self.apply(("restore", "t2"), warnings=["conversation missing"]))
        self.assertEqual(self.notifications, [("shelf", "conversation missing")])

    def test_restore_shows_its_progress_before_it_runs(self):
        seen = []
        picker.apply(self.state, ("restore", "t2"), self.arch, lambda archive_id: {"warnings": []},
                     lambda *a: None, draw=lambda: seen.append(self.state.message))
        self.assertEqual(seen, ["Restoring flaky-test..."])

    def test_failed_restore_keeps_the_popup_and_the_entry(self):
        self.assertFalse(self.apply(("restore", "t2"), restore_error=RuntimeError("boom")))
        self.assertEqual(self.state.message, "Restore failed: boom")
        self.assertEqual(current(self.state).record["id"], "t2")

    def test_duplicate_conversation_skip_is_shown(self):
        skip = archive.Skip("conversation ab12cd34 is already open in another tab; close it first")
        self.assertFalse(self.apply(("restore", "t2"), restore_error=skip))
        self.assertIn("Restore failed: conversation ab12cd34 is already open", self.state.message)
        self.assertEqual(self.arch.deleted, [])

    def test_restore_lock_busy_is_friendly(self):
        lock_path = self.arch.root.parent / "sweep.lock"
        self.assertFalse(self.apply(("restore", "t2"), restore_error=LockBusy(str(lock_path))))
        self.assertEqual(self.state.message, picker.SWEEP_BUSY)

    def test_delete_removes_the_entry(self):
        self.assertFalse(self.apply(("delete", "t2")))
        self.assertEqual(self.arch.deleted, ["t2"])
        self.assertEqual(current(self.state).record["id"], "t1")

    def test_delete_runs_under_the_sweep_lock(self):
        with FileLock(self.arch.root.parent / "sweep.lock"), \
                mock.patch("shelf.picker.DELETE_LOCK_WAIT_SECONDS", 0.1):
            self.assertFalse(self.apply(("delete", "t2")))
        self.assertEqual(self.arch.deleted, [])
        self.assertEqual(self.state.message, picker.SWEEP_BUSY)
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python3 -m unittest tests.test_picker -v`
Expected: 11 errors, such as `AttributeError: module 'shelf.picker' has no attribute 'apply'`.

- [ ] **Step 3: Implement**

Append this to `shelf/picker.py`:

```python
SWEEP_BUSY = "A sweep is running; try again in a moment."


def refresh(state: State, records: list) -> None:
    """Swap in a re-read archive, keeping the cursor on the same entry when it still exists."""
    cur = _current(state)
    cur_id = cur.record["id"] if cur and cur.kind == "tab" else None
    old = state.cursor
    _load(state, records)
    ids = [r.record["id"] if r.kind == "tab" else None for r in rows(state)]
    state.cursor = ids.index(cur_id) if cur_id in ids else old
    _follow(state)


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
```

`apply` keeps the old picker's behaviour:
- A delete runs under `sweep.lock`, waiting up to
  `DELETE_LOCK_WAIT_SECONDS`.
- Ctrl-C is ignored while a restore runs.
- A failed restore keeps both the entry and the popup.
- Restore warnings go to `notify`.
- The one change: `draw` is called before the slow work, so the status line
  shows what is happening.

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `python3 -m unittest discover -s tests -t .`
Expected: `OK` (430 tests).

- [ ] **Step 5: Commit**

```bash
git add shelf/picker.py tests/test_picker.py
git commit -m "feat: restore and delete actions for the new picker" -m "Assisted by AI"
```

---

### Task 5: Render, the curses loop, and the switch-over

This task removes the old picker (`parse_choice`, the old `render` and
`run`, `_visible_rows`, `ESC`, `CLEAR_SCREEN`) and puts the new one in its
place. Everything after it goes through the new code.

**Files:**
- Modify: `shelf/picker.py` (replace the whole file)
- Modify: `tests/test_picker.py` (replace the whole file)
- Create: `tests/test_picker_tty.py`
- Modify: `shelf/__main__.py` (`_pick`)
- Modify: `tests/test_main.py` (two tests)
- Modify: `herdr-plugin.toml`, `tests/test_manifest.py`

- [ ] **Step 1: Replace `tests/test_picker.py` with the final test file**

This drops the old `ParseTest`, `RenderTest` and `RunTest` classes and the
old `T0`/`RECORDS` fixtures. It also adds the new `RenderTest` and a no-match
render test. The classes from Tasks 1-4 are unchanged.

```python
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from shelf import archive, picker
from shelf.util import FileLock, LockBusy, iso

LOCAL = timezone(timedelta(hours=-7))  # the "local" zone for every test here
NOW = datetime(2026, 9, 29, 10, 0, tzinfo=LOCAL)  # 17:00Z
TODAY_AT = "2026-09-29T16:14:00Z"  # 09:14 local
WEEK_AT = "2026-09-25T21:41:17Z"
MONTH_AT = "2026-09-10T12:00:00Z"
OLDER_AT = "2026-08-01T12:00:00Z"


def rec(archive_id, archived_at, label, workspace="backend", agent="claude", idle=20, cwd=None):
    last = iso(NOW - timedelta(days=idle, hours=1))
    return {"id": archive_id, "archived_at": archived_at, "tab": {"label": label},
            "workspace": {"label": workspace, "cwd": cwd},
            "panes": {"p": {"agent": agent, "last_activity": last}}}


HOME_SRC = str(Path.home() / "src" / "backend")
ELEVEN = [
    rec("t1", TODAY_AT, "perf-probe", idle=21),
    rec("t2", TODAY_AT, "flaky-test", agent="codex", idle=15, cwd=HOME_SRC),
    rec("w1", WEEK_AT, "onboarding", workspace="docs", idle=28),
    rec("w2", WEEK_AT, "api-refactor", idle=17),
    rec("w3", WEEK_AT, "release-notes", workspace="docs", idle=18),
    rec("m1", MONTH_AT, "db-migration", idle=34),
    rec("m2", "2026-09-09T12:00:00Z", "style-guide", workspace="docs", idle=42),
    rec("o1", OLDER_AT, "lint-cleanup", idle=43),
    rec("o2", OLDER_AT, "api-sketch", idle=60),
    rec("o3", OLDER_AT, "old-spike", idle=32),
    rec("o4", "2026-07-01T12:00:00Z", "misc", idle=90),
]


def state_of(records=ELEVEN, height=16, width=80):
    state = picker.initial_state(records, NOW)
    state.height, state.width = height, width
    return state


def press(state, *events):
    action = None
    for event in events:
        action = picker.reduce(state, ("char", event) if isinstance(event, str) and len(event) == 1 else event)
    return action


def current(state):
    return picker.rows(state)[state.cursor]


def labels(state):
    return [r.group if r.kind == "header" else r.record["tab"]["label"] for r in picker.rows(state)]


class FakeArchive:
    def __init__(self, records, root=None):
        self.records = list(records)
        self.deleted = []
        self.root = root or Path(tempfile.mkdtemp(prefix="shelf-picker-test-")) / "archive"

    def list(self):
        return list(self.records)

    def delete(self, archive_id):
        self.deleted.append(archive_id)
        self.records = [r for r in self.records if r["id"] != archive_id]


class GroupTest(unittest.TestCase):
    def group(self, archived_at):
        return picker.group_of({"archived_at": archived_at}, NOW)

    def test_boundaries_in_local_calendar_days(self):
        cases = {
            "2026-09-29T07:00:00Z": picker.TODAY,  # 00:00 local today
            "2026-09-29T06:59:00Z": picker.WEEK,  # 23:59 local yesterday
            "2026-09-23T12:00:00Z": picker.WEEK,  # 6 days ago
            "2026-09-22T12:00:00Z": picker.MONTH,  # 7 days ago
            "2026-08-31T12:00:00Z": picker.MONTH,  # 29 days ago
            "2026-08-30T12:00:00Z": picker.OLDER,  # 30 days ago
            "2026-10-01T12:00:00Z": picker.TODAY,  # a clock that ran ahead
        }
        for archived_at, expected in cases.items():
            with self.subTest(archived_at=archived_at):
                self.assertEqual(self.group(archived_at), expected)

    def test_a_utc_timestamp_on_a_different_local_date(self):
        # 03:00Z on the 29th is 20:00 local on the 28th: yesterday, not today.
        self.assertEqual(self.group("2026-09-29T03:00:00Z"), picker.WEEK)

    def test_missing_or_unparseable_archived_at_is_older(self):
        for value in (None, "", "not-a-date"):
            with self.subTest(value=value):
                self.assertEqual(self.group(value), picker.OLDER)


class InitialStateTest(unittest.TestCase):
    def test_older_starts_collapsed_and_the_rest_open(self):
        self.assertEqual(labels(state_of()), [
            picker.TODAY, "flaky-test", "perf-probe",
            picker.WEEK, "api-refactor", "release-notes", "onboarding",
            picker.MONTH, "db-migration", "style-guide",
            picker.OLDER])

    def test_sorted_newest_archive_first_then_least_idle_first(self):
        # Within Today both came from one sweep, so the less idle one leads;
        # within Last 30 days the more recently archived one leads.
        rs = picker.rows(state_of())
        self.assertEqual([r.record["id"] for r in rs if r.kind == "tab"][:2], ["t2", "t1"])
        self.assertEqual([r.record["id"] for r in rs if r.group == picker.MONTH and r.kind == "tab"],
                         ["m1", "m2"])

    def test_older_opens_when_it_is_the_only_group(self):
        state = state_of([r for r in ELEVEN if r["id"].startswith("o")])
        self.assertEqual(labels(state), [picker.OLDER, "old-spike", "lint-cleanup", "api-sketch", "misc"])

    def test_empty_groups_are_not_shown(self):
        state = state_of([r for r in ELEVEN if r["id"] in ("t1", "o1")])
        self.assertEqual(labels(state), [picker.TODAY, "perf-probe", picker.OLDER])

    def test_cursor_starts_on_the_first_tab_row(self):
        self.assertEqual(current(state_of()).record["id"], "t2")

    def test_no_activity_timestamp_still_lists_the_entry(self):
        entry = dict(rec("x", TODAY_AT, "blank"), panes={})
        state = state_of([entry])
        self.assertEqual(labels(state), [picker.TODAY, "blank"])


class MoveTest(unittest.TestCase):
    def test_up_and_down_stop_at_the_ends(self):
        state = state_of()
        press(state, "up", "up", "up")
        self.assertEqual(state.cursor, 0)
        press(state, *["down"] * 20)
        self.assertEqual(current(state).group, picker.OLDER)

    def test_vim_keys_move_like_the_arrows(self):
        state = state_of()
        press(state, "j")
        self.assertEqual(current(state).record["id"], "t1")
        press(state, "k")
        self.assertEqual(current(state).record["id"], "t2")

    def test_page_home_and_end(self):
        state = state_of()
        press(state, "pgdn")
        self.assertEqual(state.cursor, 10)
        press(state, "pgup")
        self.assertEqual(state.cursor, 0)
        press(state, "end")
        self.assertEqual(state.cursor, len(picker.rows(state)) - 1)
        press(state, "home")
        self.assertEqual(state.cursor, 0)

    def test_enter_on_a_tab_restores_it(self):
        self.assertEqual(press(state_of(), "enter"), ("restore", "t2"))

    def test_enter_on_a_header_toggles_the_group(self):
        state = state_of()
        press(state, "end", "enter")
        self.assertIn("old-spike", labels(state))
        press(state, "enter")
        self.assertNotIn("old-spike", labels(state))

    def test_right_opens_and_left_closes(self):
        state = state_of()
        press(state, "end", "right")
        self.assertIn("old-spike", labels(state))
        press(state, "right")  # already open: stays open
        self.assertIn("old-spike", labels(state))
        press(state, "left")
        self.assertNotIn("old-spike", labels(state))

    def test_left_on_a_tab_jumps_to_its_header_and_closes_it(self):
        state = state_of()
        press(state, "down", "down", "down")  # api-refactor, under Last 7 days
        press(state, "h")
        self.assertEqual(current(state), picker.Row("header", picker.WEEK, None, 3, False))
        self.assertNotIn("api-refactor", labels(state))

    def test_q_and_esc_quit(self):
        self.assertEqual(press(state_of(), "q"), ("quit",))
        self.assertEqual(press(state_of(), "esc"), ("quit",))

    def test_scrolling_keeps_the_cursor_in_the_ten_row_window(self):
        state = state_of()
        press(state, "end", "right")  # all 15 rows showing
        press(state, "end")
        self.assertEqual((state.cursor, state.offset), (14, 5))
        press(state, *["up"] * 9)
        self.assertEqual((state.cursor, state.offset), (5, 5))
        press(state, "up")
        self.assertEqual((state.cursor, state.offset), (4, 4))

    def test_wheel_moves_three_rows(self):
        state = state_of()
        press(state, "wheeldown")
        self.assertEqual(state.cursor, 4)
        press(state, "wheelup")
        self.assertEqual(state.cursor, 1)

    def test_click_highlights_and_double_click_restores(self):
        state = state_of()
        self.assertIsNone(press(state, ("click", 5)))
        self.assertEqual(current(state).record["id"], "w3")
        self.assertEqual(press(state, ("dclick", 5)), ("restore", "w3"))

    def test_click_on_a_header_toggles_it(self):
        state = state_of()
        press(state, ("click", 0))
        self.assertEqual(current(state).group, picker.TODAY)
        self.assertEqual(current(state).kind, "header")
        self.assertNotIn("flaky-test", labels(state))

    def test_click_accounts_for_the_scroll_offset_and_ignores_empty_rows(self):
        state = state_of()
        press(state, "end", "right", "end")  # offset 5
        press(state, ("click", 0))
        self.assertEqual(current(state).record["id"], "w3")
        state = state_of()
        self.assertIsNone(press(state, ("click", 9)))  # rows 0-10 exist; row 9 is style-guide
        self.assertEqual(current(state).record["id"], "m2")
        self.assertIsNone(press(state, ("click", 11)))
        self.assertEqual(current(state).record["id"], "m2")


class WindowRowTest(unittest.TestCase):
    def test_maps_screen_lines_to_list_rows(self):
        state = state_of(height=16)
        self.assertEqual([picker.window_row(state, y) for y in (1, 2, 11, 12)], [None, 0, 9, None])
        state = state_of(height=13)  # no markers: the list starts right under the title
        self.assertEqual(picker.window_row(state, 1), 0)


class FilterTest(unittest.TestCase):
    def test_typing_narrows_and_opens_groups_with_a_match(self):
        state = state_of()
        press(state, "/", "a", "p", "i")
        self.assertEqual(state.mode, "filter")
        self.assertEqual(labels(state), [picker.WEEK, "api-refactor", picker.OLDER, "api-sketch"])
        self.assertEqual(current(state).record["id"], "w2")

    def test_matches_workspace_and_agent_names_case_insensitively(self):
        state = state_of()
        press(state, "/", "C", "O", "D", "E", "X")
        self.assertEqual(labels(state), [picker.TODAY, "flaky-test"])
        press(state, "esc", "/", "d", "o", "c", "s")
        self.assertEqual(set(labels(state)) - set(picker.GROUPS),
                         {"onboarding", "release-notes", "style-guide"})

    def test_filter_keys_are_typed_as_text(self):
        state = state_of()
        self.assertIsNone(press(state, "/", "q", "d", "j", "/"))
        self.assertEqual((state.mode, state.filter_text), ("filter", "qdj/"))

    def test_no_match(self):
        state = state_of()
        press(state, "/", "z", "z", "z")
        self.assertEqual(picker.rows(state), [])

    def test_enter_keeps_the_filter_then_esc_clears_it_then_esc_quits(self):
        state = state_of()
        press(state, "/", "a", "p", "i", "enter")
        self.assertEqual((state.mode, state.filter_text), ("move", "api"))
        self.assertEqual(press(state, "down"), None)
        self.assertEqual(current(state).group, picker.OLDER)
        self.assertIsNone(press(state, "esc"))
        self.assertEqual(state.filter_text, "")
        self.assertEqual(press(state, "esc"), ("quit",))

    def test_backspace_deletes_then_leaves_filter_mode(self):
        state = state_of()
        press(state, "/", "a", "backspace")
        self.assertEqual((state.mode, state.filter_text), ("filter", ""))
        press(state, "backspace")
        self.assertEqual(state.mode, "move")

    def test_clearing_the_filter_restores_the_open_groups(self):
        state = state_of()
        before = labels(state)
        press(state, "/", "a", "p", "i")  # opens Older
        press(state, "esc")
        self.assertEqual((state.mode, labels(state)), ("move", before))


class ConfirmTest(unittest.TestCase):
    def test_d_then_y_deletes(self):
        state = state_of()
        self.assertIsNone(press(state, "d"))
        self.assertEqual(state.mode, "confirm")
        self.assertEqual(press(state, "y"), ("delete", "t2"))
        self.assertEqual(state.mode, "move")

    def test_anything_else_cancels(self):
        for key in ("n", "N", "esc", "enter", "q", "down"):
            with self.subTest(key=key):
                state = state_of()
                press(state, "d")
                self.assertIsNone(press(state, key))
                self.assertEqual((state.mode, current(state).record["id"]), ("move", "t2"))

    def test_d_on_a_header_does_nothing(self):
        state = state_of()
        press(state, "home", "d")
        self.assertEqual(state.mode, "move")


class RefreshTest(unittest.TestCase):
    def test_keeps_the_cursor_on_the_same_entry(self):
        state = state_of()
        press(state, "down", "down", "down")  # api-refactor
        picker.refresh(state, [r for r in ELEVEN if r["id"] not in ("t1", "t2")])
        self.assertEqual(current(state).record["id"], "w2")

    def test_falls_back_to_the_same_position_when_the_entry_is_gone(self):
        state = state_of()
        press(state, "down")  # perf-probe, row 2
        picker.refresh(state, [r for r in ELEVEN if r["id"] != "t1"])
        self.assertEqual(state.cursor, 2)
        self.assertEqual(current(state).kind, "header")


class RenderTest(unittest.TestCase):
    maxDiff = None

    def test_the_popup_as_it_opens(self):
        lines, highlight = picker.render(state_of())
        self.assertEqual(lines, [
            " Shelf - 11 archived",
            "",
            " - Archived today (2)",
            " > flaky-test                         backend                 codex        15d",
            "   perf-probe                         backend                 claude       21d",
            " - Last 7 days (3)",
            "   api-refactor                       backend                 claude       17d",
            "   release-notes                      docs                    claude       18d",
            "   onboarding                         docs                    claude       28d",
            " - Last 30 days (2)",
            "   db-migration                       backend                 claude       34d",
            "   style-guide                        docs                    claude       42d",
            " v 1 more",
            " ~/src/backend  shelved Sep 29 09:14  1 pane: codex",
            picker.KEYS_HINT[:79],
        ])
        self.assertEqual(highlight, 3)

    def test_scroll_markers(self):
        state = state_of()
        press(state, "end", "right", "end")
        lines, highlight = picker.render(state)
        self.assertEqual((lines[1], lines[12]), (" ^ 5 more", ""))
        self.assertEqual(highlight, 11)
        press(state, "home")
        lines, _ = picker.render(state)
        self.assertEqual((lines[1], lines[12]), ("", " v 5 more"))

    def test_details_line_is_blank_on_a_header(self):
        state = state_of()
        press(state, "home")
        self.assertEqual(picker.render(state)[0][13], "")

    def test_narrow_popup_truncates_every_line(self):
        lines, _ = picker.render(state_of(width=40))
        self.assertTrue(all(len(line) <= 39 for line in lines))

    def test_short_popups_drop_details_then_markers_then_list_rows(self):
        self.assertEqual(len(picker.render(state_of(height=15))[0]), 15)
        self.assertEqual(len(picker.render(state_of(height=14))[0]), 14)  # no details
        lines, _ = picker.render(state_of(height=12))  # no markers
        self.assertEqual((len(lines), lines[1]), (12, " - Archived today (2)"))
        self.assertEqual(len(picker.render(state_of(height=6))[0]), 6)

    def test_too_small(self):
        self.assertEqual(picker.render(state_of(height=4))[0], [picker.TOO_SMALL])
        self.assertEqual(picker.render(state_of(width=29))[0], [picker.TOO_SMALL])

    def test_no_match(self):
        state = state_of()
        press(state, "/", "z", "z", "z")
        lines, highlight = picker.render(state)
        self.assertEqual((lines[2], highlight), (" No match", None))

    def test_empty_archive(self):
        state = state_of([])
        self.assertEqual(picker.render(state)[0], ["No archived tabs.", "Press any key to close."])
        self.assertEqual(press(state, "x"), ("quit",))

    def test_status_line_for_confirm_filter_and_message(self):
        state = state_of()
        press(state, "d")
        self.assertEqual(picker.render(state)[0][-1],
                         ' Delete "flaky-test"? This cannot be undone. [y/N]')
        press(state, "n", "/", "a", "p")
        self.assertEqual(picker.render(state)[0][-1], " /ap")
        press(state, "enter")
        self.assertEqual(picker.render(state)[0][-1], " /ap  (Esc clears the filter)")
        state.message = "Restore failed: boom"
        self.assertEqual(picker.render(state)[0][-1], " Restore failed: boom")
        press(state, "down")  # a message lasts until the next key press
        self.assertEqual(picker.render(state)[0][-1], " /ap  (Esc clears the filter)")


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.arch = FakeArchive(ELEVEN)
        self.state = state_of(self.arch.list())
        self.restored, self.notifications = [], []

    def apply(self, action, restore_error=None, warnings=()):
        def do_restore(archive_id):
            self.restored.append(archive_id)
            if restore_error:
                raise restore_error
            return {"tab_id": "t", "warnings": list(warnings)}

        return picker.apply(self.state, action, self.arch, do_restore,
                            lambda title, body: self.notifications.append((title, body)))

    def test_quit_and_no_action(self):
        self.assertTrue(self.apply(("quit",)))
        self.assertFalse(self.apply(None))

    def test_restore_closes_the_popup(self):
        self.assertTrue(self.apply(("restore", "t2")))
        self.assertEqual((self.restored, self.notifications), (["t2"], []))

    def test_restore_warnings_are_notified(self):
        self.assertTrue(self.apply(("restore", "t2"), warnings=["conversation missing"]))
        self.assertEqual(self.notifications, [("shelf", "conversation missing")])

    def test_restore_shows_its_progress_before_it_runs(self):
        seen = []
        picker.apply(self.state, ("restore", "t2"), self.arch, lambda archive_id: {"warnings": []},
                     lambda *a: None, draw=lambda: seen.append(self.state.message))
        self.assertEqual(seen, ["Restoring flaky-test..."])

    def test_failed_restore_keeps_the_popup_and_the_entry(self):
        self.assertFalse(self.apply(("restore", "t2"), restore_error=RuntimeError("boom")))
        self.assertEqual(self.state.message, "Restore failed: boom")
        self.assertEqual(current(self.state).record["id"], "t2")

    def test_duplicate_conversation_skip_is_shown(self):
        skip = archive.Skip("conversation ab12cd34 is already open in another tab; close it first")
        self.assertFalse(self.apply(("restore", "t2"), restore_error=skip))
        self.assertIn("Restore failed: conversation ab12cd34 is already open", self.state.message)
        self.assertEqual(self.arch.deleted, [])

    def test_restore_lock_busy_is_friendly(self):
        lock_path = self.arch.root.parent / "sweep.lock"
        self.assertFalse(self.apply(("restore", "t2"), restore_error=LockBusy(str(lock_path))))
        self.assertEqual(self.state.message, picker.SWEEP_BUSY)

    def test_delete_removes_the_entry(self):
        self.assertFalse(self.apply(("delete", "t2")))
        self.assertEqual(self.arch.deleted, ["t2"])
        self.assertEqual(current(self.state).record["id"], "t1")

    def test_delete_runs_under_the_sweep_lock(self):
        with FileLock(self.arch.root.parent / "sweep.lock"), \
                mock.patch("shelf.picker.DELETE_LOCK_WAIT_SECONDS", 0.1):
            self.assertFalse(self.apply(("delete", "t2")))
        self.assertEqual(self.arch.deleted, [])
        self.assertEqual(self.state.message, picker.SWEEP_BUSY)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Create `tests/test_picker_tty.py`**

This test forks a child on a pseudo-terminal that runs the real `run()`,
waits for the first frame, and sends raw key bytes. It checks three things:
- Down then Enter restores the second tab, sent both as `ESC O B` (what
  curses decodes in keypad mode) and as `ESC [ B` (which curses leaves
  undecoded).
- A single Esc closes the popup in well under ncurses' 1-second default Esc
  delay.

```python
"""End to end: the curses loop in a real pseudo-terminal."""

import fcntl
import os
import pty
import select
import struct
import tempfile
import termios
import time
import traceback
import unittest
from datetime import datetime, timezone
from pathlib import Path

from shelf import picker

try:
    import curses  # noqa: F401
except ImportError:  # a Python built without curses
    curses = None

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
RECORDS = [
    {"id": "first", "archived_at": "2026-09-23T12:00:00Z", "tab": {"label": "first-tab"}, "workspace": {},
     "panes": {"p": {"agent": "claude", "last_activity": "2026-09-09T12:00:00Z"}}},
    {"id": "second", "archived_at": "2026-09-23T11:00:00Z", "tab": {"label": "second-tab"}, "workspace": {},
     "panes": {"p": {"agent": "claude", "last_activity": "2026-09-09T12:00:00Z"}}},
]


class FakeArchive:
    def __init__(self, root):
        self.root = root

    def list(self):
        return list(RECORDS)

    def delete(self, archive_id):
        pass


@unittest.skipIf(curses is None, "needs curses")
class TtyTest(unittest.TestCase):
    def run_in_pty(self, keys):
        """Run picker.run() in a child on a pty, send `keys`; return the restored id or None."""
        tmp = Path(tempfile.mkdtemp(prefix="shelf-tty-test-"))
        result = tmp / "restored"
        pid, fd = pty.fork()
        if pid == 0:  # child: never return into the test runner
            code = 1
            try:
                os.environ["TERM"] = "xterm-256color"
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 16, 80, 0, 0))

                def do_restore(archive_id):
                    result.write_text(archive_id)
                    return {"warnings": []}

                picker.run(FakeArchive(tmp / "archive"), do_restore, lambda: T0)
                code = 0
            except BaseException:
                traceback.print_exc()
            finally:
                os._exit(code)
        output = b""
        sent = False
        deadline = time.monotonic() + 10
        status = None
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    output += os.read(fd, 4096)
                except OSError:  # the child exited and closed the pty
                    pass
            if not sent and b"first-tab" in output:
                os.write(fd, keys)
                sent = True
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
        else:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            self.fail(f"picker did not exit; output: {output[-500:]!r}")
        os.close(fd)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, output[-2000:])
        return result.read_text() if result.exists() else None

    def test_down_then_enter_restores_the_second_tab(self):
        self.assertEqual(self.run_in_pty(b"\x1bOB\r"), "second")

    def test_an_undecoded_arrow_is_not_read_as_esc(self):
        self.assertEqual(self.run_in_pty(b"\x1b[B\r"), "second")

    def test_one_esc_closes_the_popup(self):
        started = time.monotonic()
        self.assertIsNone(self.run_in_pty(b"\x1b"))
        self.assertLess(time.monotonic() - started, 0.6)  # ncurses' default Esc delay alone is 1 s


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Update the `pick` tests in `tests/test_main.py`**

Replace the test `test_pick_passes_input_fn_explicitly_so_mocking_builtins_input_works`
(the whole method) with:

```python
    def test_pick_keeps_log_lines_off_the_popup_while_it_runs(self):
        seen = []

        def fake_run(arch, do_restore, now_fn, notify=None, **kwargs):
            seen.append([type(h) for h in logging.getLogger("shelf").handlers])

        with mock.patch("shelf.__main__.picker.run", side_effect=fake_run), \
                mock.patch("shelf.__main__.Client"), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["pick"]), 0)
        self.assertEqual(seen, [[logging.FileHandler]])
        self.assertIn(logging.StreamHandler, [type(h) for h in logging.getLogger("shelf").handlers])
```

In `test_pick_lists_archives_from_the_session_dir_not_the_root`, replace:

```python
        out = io.StringIO()
        with mock.patch("builtins.input", return_value="q"), redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["pick"]), 0)
        self.assertIn("distinctive-label", out.getvalue())
```

with:

```python
        listed = []
        with mock.patch("shelf.__main__.picker.run",
                        side_effect=lambda arch, *a, **k: listed.extend(arch.list())), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(main(["pick"]), 0)
        self.assertEqual([r["tab"]["label"] for r in listed], ["distinctive-label"])
```

In `tests/test_manifest.py`, add after the `placement` assertion:

```python
        self.assertEqual((m["panes"][0]["width"], m["panes"][0]["height"]), ("80%", 18))
```

- [ ] **Step 4: Run the tests to make sure they fail**

Run: `python3 -m unittest tests.test_picker tests.test_picker_tty tests.test_main -v`
Expected:
- errors such as `TypeError: render() missing 1 required positional argument: 'now'`;
- the three `TtyTest` tests failing, each after up to 10 s: the old `input()` picker never sees a
  valid choice in the key bytes;
- `test_pick_keeps_log_lines_off_the_popup_while_it_runs` failing.

- [ ] **Step 5: Replace `shelf/picker.py` with the final module**

It contains everything from Tasks 1-4 unchanged, plus `render` and its
helpers and the curses loop.

```python
"""The popup that lists archived tabs and restores one.

The picker logic is pure (State, reduce, render, apply) so it is tested
without a terminal; run() is a thin curses loop around it.
"""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass, field
from datetime import datetime
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
KEYS_HINT = " Up/Down move  Enter restore  Right/Left open/close  / filter  d delete  q quit"
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


def group_of(record: dict, now: datetime) -> str:
    """The record's group, by local calendar days since archived_at. `now` is aware local time."""
    at = parse_iso(record.get("archived_at"))
    if at is None:
        return OLDER
    days = (now.date() - at.astimezone(now.tzinfo).date()).days
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
        state.grouped[group_of(rec, state.now)].append(rec)


def initial_state(records: list, now: datetime) -> State:
    state = State(now=now)
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


def layout(height: int):
    """(list rows, show the scroll markers, show the details line) for a popup `height` rows tall."""
    if height >= LIST_ROWS + 5:
        return LIST_ROWS, True, True
    if height >= LIST_ROWS + 4:
        return LIST_ROWS, True, False
    return max(min(LIST_ROWS, height - 2), 1), False, False


def window_row(state: State, y: int):
    """The list-window row at screen line `y`, or None when `y` is outside the list."""
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


def refresh(state: State, records: list) -> None:
    """Swap in a re-read archive, keeping the cursor on the same entry when it still exists."""
    cur = _current(state)
    cur_id = cur.record["id"] if cur and cur.kind == "tab" else None
    old = state.cursor
    _load(state, records)
    ids = [r.record["id"] if r.kind == "tab" else None for r in rows(state)]
    state.cursor = ids.index(cur_id) if cur_id in ids else old
    _follow(state)


def reduce(state: State, event):
    """Apply one input event to `state` (in place); return an action or None.

    Events: "up", "down", "pgup", "pgdn", "home", "end", "enter", "left",
    "right", "backspace", "esc", "wheelup", "wheeldown", ("char", c),
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
            _set_filter(state, "")
            state.mode = "move"
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


def _details(record: dict, now: datetime) -> str:
    cwd = record.get("workspace", {}).get("cwd") or ""
    home = str(Path.home())
    if cwd == home or cwd.startswith(home + os.sep):
        cwd = "~" + cwd[len(home):]
    at = parse_iso(record.get("archived_at"))
    shelved = ""
    if at is not None:
        local = at.astimezone(now.tzinfo)
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
    if state.height < 5 or state.width < 30:
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
        lines.append(_details(cur.record, state.now) if cur and cur.kind == "tab" else "")
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
    screen.keypad(True)
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
            screen.addstr(y, 0, line.ljust(state.width - 1) if y == highlight else line, attr)
        except curses.error:
            pass  # a line wider than the screen (e.g. wide characters)
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
        return ("char", key) if key.isprintable() else None
    if key == curses.KEY_RESIZE:
        return ("resize",) + screen.getmaxyx()
    if key == curses.KEY_MOUSE:
        return _mouse_event(curses, state)
    return {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right",
            curses.KEY_PPAGE: "pgup", curses.KEY_NPAGE: "pgdn", curses.KEY_HOME: "home", curses.KEY_END: "end",
            curses.KEY_ENTER: "enter", curses.KEY_BACKSPACE: "backspace"}.get(key)


def _after_escape(curses, screen):
    """Tell a lone Esc from an arrow key curses did not decode (ESC [ B or ESC O B)."""
    screen.timeout(50)
    try:
        nxt = _poll(curses, screen)
        if nxt not in ("[", "O"):
            return "esc"
        final = _poll(curses, screen)
        return {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}.get(final)
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
    wheel_down = getattr(curses, "BUTTON5_PRESSED", 0)  # not exported by every Python build
    if buttons & curses.BUTTON4_PRESSED:
        return "wheelup"
    if wheel_down and buttons & wheel_down:
        return "wheeldown"
    row = window_row(state, y)
    if row is None:
        return None
    if buttons & curses.BUTTON1_DOUBLE_CLICKED:
        return ("dclick", row)
    if buttons & curses.BUTTON1_CLICKED:
        return ("click", row)
    return None
```

How the loop works:
- **Esc delay:** `run()` sets `ESCDELAY` before importing curses, because
  ncurses reads it once at start-up.
- **Esc versus arrows:** `_after_escape` waits up to 50 ms after a lone Esc.
  It reads `[` or `O` plus a final letter as an arrow, so an undecoded
  `ESC [ B` never closes the popup.
- **Last column:** `_draw` never writes the last column, because curses
  raises on the bottom-right cell. It also swallows `curses.error` from
  wide characters.

- [ ] **Step 6: Update `_pick` in `shelf/__main__.py`**

Replace:

```python
        # input_fn and print_fn are passed explicitly (rather than relying
        # on picker.run's own defaults) so a test's mock.patch("builtins.
        # input"/"builtins.print") actually takes effect: a default
        # parameter value is bound once when picker.py is first imported,
        # long before any test patches builtins.input, so picker.run's own
        # default would keep calling the original, real input() no matter
        # what is patched later.
        picker.run(arch, do_restore, now, input_fn=input, print_fn=print, notify=notify)
```

with:

```python
        # The popup's terminal belongs to curses while it runs: a log line on
        # stderr (a restore logs several) would draw over it. shelf.log
        # still gets every line.
        on_stderr = [h for h in log.handlers if type(h) is logging.StreamHandler]
        for handler in on_stderr:
            log.removeHandler(handler)
        try:
            picker.run(arch, do_restore, now, notify=notify)
        finally:
            for handler in on_stderr:
                log.addHandler(handler)
```

`type(h) is logging.StreamHandler` is deliberate: `FileHandler` subclasses
`StreamHandler`, and the file handler must stay.

- [ ] **Step 7: Set the popup height**

In `herdr-plugin.toml`, under `[[panes]]`, change `height = "80%"` to
`height = 18`. Leave `width = "80%"`.

- [ ] **Step 8: Run the whole suite under both Pythons**

Run: `python3.9 -m unittest discover -s tests -t .` and `python3.12 -m unittest discover -s tests -t .`
Expected: `OK` (423 tests) on 3.12. On 3.9 you get `OK (skipped=1)`, because
the manifest test needs `tomllib`.

- [ ] **Step 9: Commit**

```bash
git add shelf/picker.py shelf/__main__.py tests/test_picker.py tests/test_picker_tty.py tests/test_main.py tests/test_manifest.py herdr-plugin.toml
git commit -m "feat: curses picker with archive-date groups replaces the numbered prompt" -m "Assisted by AI"
```

---

### Task 6: Docs, changelog and version

**Files:** `README.md`, `AGENTS.md`, `CHANGELOG.md`, `shelf/__init__.py`,
`herdr-plugin.toml`, `docs/superpowers/specs/2026-09-24-herdr-shelf-design.md`

- [ ] **Step 1: README "Restore" section**

Replace this paragraph:

```markdown
Press your picker key. The popup lists archived tabs, newest first, as many
as fit the popup. When more archived tabs exist than fit, restore the rest
with `python3 -m shelf restore <id>` (ids from `python3 -m shelf list`). Type
a number to restore one, `d <number>` to delete one, `q` to close, or Esc
then Enter to close. Deleting asks for confirmation (`[y/N]`, default no) and
is permanent: there is no undo.
```

with:

```markdown
Press your picker key. The popup groups archived tabs by when they were
archived: Archived today, Last 7 days, Last 30 days, and Older. Older starts
collapsed, unless it is the only group. The list shows 10 rows and scrolls.
A line under it shows the highlighted tab's directory, when it was shelved,
and its panes.

| Key | Does |
|---|---|
| Up/Down (or k/j), PgUp/PgDn, Home/End | Move |
| Enter | Restore the highlighted tab, or open/close a group |
| Right/Left (or l/h) | Open/close a group |
| `/` | Filter by tab, workspace or agent name; Enter keeps it, Esc clears it |
| `d` | Delete the highlighted tab |
| q or Esc | Close |

A click highlights a row, a double-click restores it, a click on a group
header opens or closes it, and the wheel scrolls. Deleting asks for
confirmation (`[y/N]`, default no) and is permanent: there is no undo.
```

- [ ] **Step 2: AGENTS.md module map**

Replace the line
`- `shelf/picker.py`: popup UI listing archived tabs (restore/delete/quit).`
with:

```markdown
- `shelf/picker.py`: the restore popup. Pure logic (`State`, `reduce`,
  `render`, `apply`) tested without a terminal, plus a thin curses loop
  (`run`); `tests/test_picker_tty.py` drives that loop in a pseudo-terminal.
```

- [ ] **Step 3: The original design spec's picker section**

In `docs/superpowers/specs/2026-09-24-herdr-shelf-design.md`, replace the
body of `### Picker (`pick`)`, from "Opened by the `restore` action" through
"It uses plain `input()` with no curses dependency.", with:

```markdown
Opened by the `restore` action through `plugin.pane.open`. A curses popup,
18 rows tall. It groups archived tabs by when they were archived (Archived
today, Last 7 days, Last 30 days, Older; Older collapsed). It has arrow keys,
Enter to restore, `d` to delete, `/` to filter, mouse support, and q or Esc
to close. See `2026-09-29-picker-redesign-design.md` for the full design.
```

- [ ] **Step 4: CHANGELOG**

Insert this above `## 0.3.0 - 2026-09-26`:

```markdown
## 0.4.0 - unreleased

- Redesign the restore picker as an arrow-key popup. Archived tabs are
  grouped by when they were archived: Archived today, Last 7 days, Last 30
  days, and Older, which starts collapsed. The list shows 10 rows and
  scrolls, so every entry is reachable from the popup. Enter restores,
  Right/Left open and close a group, `/` filters by tab, workspace or agent
  name, `d` deletes after a `[y/N]` confirmation, and one press of q or Esc
  closes it. A details line shows the highlighted tab's directory, when it
  was shelved, and its panes. Clicks, double-clicks and the wheel work too.
- **Behavior change:** the number shortcuts (`3` to restore, `d 3` to delete)
  are gone, and the popup is now 18 rows tall instead of 80% of the screen.
- The picker now uses Python's built-in `curses` module.
```

- [ ] **Step 5: Version**

Set `__version__ = "0.4.0"` in `shelf/__init__.py` and `version = "0.4.0"`
in `herdr-plugin.toml`. `tests/test_manifest.py` checks that the two match.

- [ ] **Step 6: Run the suite and commit**

Run: `python3 -m unittest discover -s tests -t .`
Expected: `OK`.

```bash
git add README.md AGENTS.md CHANGELOG.md shelf/__init__.py herdr-plugin.toml docs/superpowers/specs/2026-09-24-herdr-shelf-design.md
git commit -m "docs: document the new picker; version 0.4.0" -m "Assisted by AI"
```

---

### Task 7: Verify for real, then squash

- [ ] **Step 1: Both Pythons, one more time**

Run: `python3.9 -m unittest discover -s tests -t .` and `python3.12 -m unittest discover -s tests -t .`
Expected: `OK` on both.

- [ ] **Step 2: Live check inside herdr (Linux machine running herdr)**

Run this from a herdr pane, in this checkout. It archives a scratch shell tab,
so no real archive entry is touched:

```bash
WS=$(herdr workspace list | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["workspaces"][0]["workspace_id"])')
for n in 1 2; do
  TAB=$(herdr tab create --workspace "$WS" --label "shelf-scratch-$n" --no-focus | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["tab"]["tab_id"])')
  python3 -m shelf archive "$TAB"
done
python3 -m shelf list | grep shelf-scratch
```

Expected: two `shelf-scratch-N` lines, both under "Archived today" in the
picker.

Open a new herdr tab in this checkout and run `python3 -m shelf pick`. Check
each of these by hand:
1. The list shows the groups. Up/Down, Home/End and PgUp/PgDn move.
2. Right and Left open and close "Older", if it exists.
3. `/scratch` narrows the list to the two scratch tabs; Esc clears the
   filter, and a second Esc closes the picker. Reopen it.
4. `d` on `shelf-scratch-2`: `n` keeps it. Press `d` again, then `y`, and it
   is gone.
5. Enter on `shelf-scratch-1` shows "Restoring shelf-scratch-1...", the
   picker closes, and the tab is back in its workspace.
6. A click highlights a row, and the wheel moves the cursor.

Afterwards, close the restored `shelf-scratch-1` tab.

- [ ] **Step 3: macOS**

With the `python3` that herdr uses on the Mac, run
`python3 -c "import curses; print(curses.ncurses_version)"`, then
`python3 -m unittest tests.test_picker_tty -v` in a checkout. Expected: the
version prints, and the three tests pass.

- [ ] **Step 4: Squash the task commits into one**

```bash
git reset --soft main
git commit -m "feat: arrow-key picker grouped by archive date" -m "Replace the numbered input() restore picker with a curses popup:
arrow keys, Enter to restore, tabs grouped by when they were
archived (Archived today, Last 7 days, Last 30 days, Older; Older
collapsed), a 10-row scrolling window, a details line, a / filter
and mouse support. The logic is a pure reducer and renderer,
tested without a terminal; a pty test drives the curses loop.

The popup is now 18 rows tall, and the number shortcuts are gone.
The stderr log handler is detached while the popup runs, so log
lines no longer draw over it." -m "Assisted by AI"
git log --oneline -3
```

Don't push. Merging, pushing, tagging 0.4.0 and reinstalling the plugin are
separate steps for the maintainer.
