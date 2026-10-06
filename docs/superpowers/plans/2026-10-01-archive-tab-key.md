# Archive the Current Tab With a Key Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A key bound to a new `shelf.archive-tab` action asks, in a small popup, whether to archive the current tab, showing when it was last active and warning about anything unusual, and archives it on `y`.

**Architecture:** `sweep.assess()` splits what a sweep refuses into blocks (the entry could not be restored correctly) and warnings (the user decides). `open-archive`, the action's command, previews the tab and opens an `archive-confirm` popup with the question and the tab's terminal ids in its env. `confirm-archive`, the popup's command, prints the question, reads one raw key (`shelf/confirm.py`), and calls `archive_now(confirmed=True, terminals=...)`, which finds the tab again by its terminal ids.

**Tech Stack:** Python 3.9+ standard library (`termios`, `tty`, `textwrap`, `unittest`); herdr 0.9.x plugin manifest and socket API.

**Spec:** `docs/superpowers/specs/2026-10-01-archive-tab-key-design.md`

---

## Before you start

- Work in the repo root (`herdr-shelf`). Create a branch from `main`: `git switch -c archive-tab-key`.
- Never `git add -A` or `git add .`; add exactly the files each commit step names.
- Don't change git config; the author identity is already set.
- The repo is public. Nothing about the developer's machines, employer or internal tools goes into it.
- Python 3.9 features only (no `match`, no `X | Y` types at runtime; `from __future__ import annotations` is already at the top of the modules that use annotations).
- Full suite: `python3 -m unittest discover -s tests -t .`. Also run it with `/usr/bin/python3.9` when that exists. `tests/test_manifest.py` needs Python 3.11+ (`tomllib`) and is skipped on 3.9.
- Every "replace" block below matches its file exactly once. If one doesn't, stop and report it rather than guessing.

## File structure

- `shelf/sweep.py` (modify): `_activity_line()`, `assess()`, `_snapshot()`, `_match()`, `preview()`; `archive_now()` gains `terminals` and `confirmed` and no longer refuses the focused tab.
- `shelf/confirm.py` (create): the popup's text (`lines()`) and its one-key raw read (`read_key()`).
- `shelf/__main__.py` (modify): the `open-archive` and `confirm-archive` commands, `_notify()`, and `_logs_off_stderr()` factored out of `_pick()`.
- `herdr-plugin.toml` (modify): the `archive-tab` action and the `archive-confirm` popup; version 0.5.0.
- Tests: `tests/test_sweep.py`, `tests/test_confirm.py` (create), `tests/test_main.py`, `tests/test_manifest.py`.
- Docs: `README.md`, `CHANGELOG.md`, `AGENTS.md`; `shelf/__init__.py` version 0.5.0.

### Task 1: `sweep.assess`: blocks, warnings and the activity line

**Files:**
- Modify: `shelf/sweep.py` (after `decide()`)
- Test: `tests/test_sweep.py` (new `AssessTest`)

`assess()` is what the popup is built from. It takes the same inputs as `decide()` and returns `(blocks, warnings, activity_line)`. A block means the archive entry could not be restored correctly; everything else a sweep would refuse is only a warning, because the user is asked first.

- [ ] **Step 1: Write the failing test**

In `tests/test_sweep.py`, insert this immediately before the line `class RunTest(unittest.TestCase):` (keep the blank lines shown):

````python
class AssessTest(unittest.TestCase):
    table = agents.table()

    def assess(self, panes, days_ago=10, open_in=None):
        tab = {"tab_id": "w1:t1", "focused": True}
        activity_of = lambda a, v, terminal_id=None: None if days_ago is None else T0 - timedelta(days=days_ago)
        return sweep.assess(tab, panes, self.table, activity_of, T0, open_in or {})

    def test_activity_line(self):
        cases = [(0, "Last activity today."), (1, "Last activity 1 day ago."),
                 (12, "Last activity 12 days ago."), (-1, "Last activity today."),
                 (None, "No activity recorded for this tab yet.")]
        for days_ago, expected in cases:
            with self.subTest(days_ago=days_ago):
                self.assertEqual(self.assess([pane("p1")], days_ago=days_ago), ([], [], expected))

    def test_activity_line_uses_the_most_recent_pane(self):
        stamps = {"A": T0 - timedelta(days=9), "B": T0 - timedelta(days=2)}
        activity_of = lambda a, v, terminal_id=None: stamps[v]
        tab = {"tab_id": "w1:t1"}
        result = sweep.assess(tab, [pane("p1", session="A"), pane("p2", session="B")], self.table,
                              activity_of, T0, {})
        self.assertEqual(result[2], "Last activity 2 days ago.")

    def test_focused_and_recent_are_not_warnings(self):
        self.assertEqual(self.assess([pane("p1")], days_ago=0)[:2], ([], []))

    def test_warnings(self):
        cases = [
            ("working", [pane("p1", status="working")], {}, "A pane is still working; archiving stops it."),
            ("working shell", [pane("p1"), pane("p2", agent=None, status="working")], {},
             "A pane is still working; archiving stops it."),
            ("shell only", [pane("p1", agent=None)], {}, "No agent in this tab; it comes back as shells."),
            ("no session", [pane("p1", session=None)], {}, "Pane p1 has no session id; it comes back as a shell."),
            ("unknown agent", [pane("p1", agent="mystery")], {},
             "Pane p1 runs mystery, which shelf cannot resume; it comes back as a shell."),
            ("two panes", [pane("p1", session="S"), pane("p2", session="S")], {},
             "Conversation S is open in two panes here; both come back resuming it."),
            ("another tab", [pane("p1", session="S")], {"open_in": {"claude:S": {"w1:t1", "w1:t9"}}},
             "Conversation S is also open in another tab; restore waits until that copy is closed."),
        ]
        for name, panes, kw, expected in cases:
            with self.subTest(name):
                blocks, warnings, _ = self.assess(panes, **kw)
                self.assertEqual(blocks, [])
                self.assertEqual(warnings, [expected])

    def test_own_tab_in_open_in_is_not_a_warning(self):
        self.assertEqual(self.assess([pane("p1", session="S")], open_in={"claude:S": {"w1:t1"}})[1], [])

    def test_several_warnings_at_once(self):
        panes = [pane("p1", status="working"), pane("p2", session=None)]
        self.assertEqual(self.assess(panes)[1], ["A pane is still working; archiving stops it.",
                                                 "Pane p2 has no session id; it comes back as a shell."])

    def test_blocks(self):
        mismatched = pane("p1")
        mismatched["agent"] = "codex"
        cases = [("invalid session id", [pane("p1", session="-rf")], "p1: invalid session id"),
                 ("agent mismatch", [mismatched], "p1: agent does not match its session")]
        for name, panes, expected in cases:
            with self.subTest(name):
                self.assertEqual(self.assess(panes)[0], [expected])
````

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest tests.test_sweep.AssessTest -v`

Expected: `FAILED (errors=18)` (Ran 7 tests). Failing: `test_activity_line`, `test_activity_line_uses_the_most_recent_pane`, `test_blocks`, `test_focused_and_recent_are_not_warnings`, .... Reason: `AttributeError: module 'shelf.sweep' has no attribute 'assess'`.

- [ ] **Step 3: Implement**

In `shelf/sweep.py`, insert this immediately after the line `return None` (keep the blank lines shown):

````python
def _activity_line(stamps: list, now: datetime) -> str:
    if not stamps:
        return "No activity recorded for this tab yet."
    days = max(0, (now - max(stamps)).days)  # a stamp in the future (clock skew) counts as today
    if days == 0:
        return "Last activity today."
    return f"Last activity {days} day{'' if days == 1 else 's'} ago."


def assess(tab: dict, panes: list, table: dict, activity_of, now: datetime, open_in: dict):
    """(blocks, warnings, activity_line) for archiving one tab on request.

    Unlike decide(), most of what a sweep refuses is only a warning here:
    the user is asked first, and the record still restores correctly. A
    block is something that would make the record restore the wrong
    conversation, or never restore at all.
    """
    blocks, warnings, stamps, seen = [], [], [], set()
    if any(p.get("agent_status") == "working" for p in panes):
        warnings.append("A pane is still working; archiving stops it.")
    agent_panes = [p for p in panes if p.get("agent")]
    if not agent_panes:
        warnings.append("No agent in this tab; it comes back as shells.")
    for p in agent_panes:
        session = p.get("agent_session") or {}
        agent, value = session.get("agent"), session.get("value")
        if not value:
            warnings.append(f"Pane {p['pane_id']} has no session id; it comes back as a shell.")
            continue
        if agent != p.get("agent"):
            # capture() would record the other agent's old session, and
            # restore would resume that conversation in this pane's place.
            blocks.append(f"{p['pane_id']}: agent does not match its session")
            continue
        if agent not in table:
            warnings.append(f"Pane {p['pane_id']} runs {agent}, which shelf cannot resume; "
                            "it comes back as a shell.")
            continue
        if not agents.valid_session_value(agent, value):
            blocks.append(f"{p['pane_id']}: invalid session id")  # agents.relaunch_argv refuses it on restore
            continue
        key = activity.session_key(agent, value)
        if key in seen:
            warnings.append(f"Conversation {value[:8]} is open in two panes here; both come back resuming it.")
        elif open_in.get(key, set()) - {tab.get("tab_id")}:
            warnings.append(f"Conversation {value[:8]} is also open in another tab; "
                            "restore waits until that copy is closed.")
        seen.add(key)
        last = activity_of(agent, value, p.get("terminal_id"))
        if last is not None:
            stamps.append(last)
    return blocks, warnings, _activity_line(stamps, now)
````

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_sweep.AssessTest -v`

Expected: `Ran 7 tests`, `OK`.

Then the whole suite. Run: `python3 -m unittest discover -s tests -t .` (and `/usr/bin/python3.9 -m unittest discover -s tests -t .` if it exists)

Expected: `Ran 462 tests`, `OK` on 3.12; `OK (skipped=1)` on 3.9 (the manifest test needs 3.11+).

- [ ] **Step 5: Commit**

```bash
git add shelf/sweep.py tests/test_sweep.py
git commit -m "feat: assess a tab for archiving on request"
```

### Task 2: `archive_now` by terminal ids, `confirmed`, and `preview`

**Files:**
- Modify: `shelf/sweep.py` (replace `archive_now()` at the end of the file)
- Test: `tests/test_sweep.py` (`RunTest`: replace `test_archive_now_refuses_focused_and_missing`)

`_snapshot()` is the gather-and-activity prelude `archive_now()` already had, now shared with `preview()`. `_match()` finds the tab by its terminal ids when given, because herdr's tab ids are positions and shift when another tab closes. `confirmed=True` refuses only on `assess()`'s blocks. Both paths stop refusing the focused tab. The old test that expected that refusal is replaced.

- [ ] **Step 1: Write the failing test**

In `tests/test_sweep.py`, replace:

````python
    def test_archive_now_refuses_focused_and_missing(self):
        with self.assertRaises(archive.Skip):
            sweep.archive_now(Client(self.fake.path), self.cfg, self.state, agents.table(), "w1:t2", now=T0)
        with self.assertRaises(archive.Skip):
            sweep.archive_now(Client(self.fake.path), self.cfg, self.state, agents.table(), "w1:t404", now=T0)
````

with:

````python
    def archive_now(self, tab_id, **kw):
        return sweep.archive_now(Client(self.fake.path), self.cfg, self.state, agents.table(), tab_id, now=T0, **kw)

    def test_archive_now_archives_the_focused_tab(self):
        self.assertTrue(self.archive_now("w1:t2"))
        self.assertIn(("tab.close", {"tab_id": "w1:t2"}), self.fake.calls)

    def test_archive_now_refuses_a_missing_tab(self):
        with self.assertRaisesRegex(archive.Skip, "no tab w1:t404"):
            self.archive_now("w1:t404")

    def test_archive_now_unconfirmed_still_refuses_a_working_tab(self):
        self.panes[0]["agent_status"] = "working"
        with self.assertRaisesRegex(archive.Skip, "working"):
            self.archive_now("w1:t1")

    def test_archive_now_confirmed_archives_through_warnings(self):
        self.panes[0]["agent_status"] = "working"
        self.assertTrue(self.archive_now("w1:t1", confirmed=True))
        self.assertIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)

    def test_archive_now_confirmed_still_refuses_a_block(self):
        self.panes[0]["agent_session"]["value"] = "-rf"
        with self.assertRaisesRegex(archive.Skip, "invalid session id"):
            self.archive_now("w1:t1", confirmed=True)
        self.assertNotIn("tab.close", self.fake.methods())

    def test_archive_now_finds_the_tab_by_terminals_after_its_id_shifts(self):
        # The popup was opened for "w1:t5"; since then herdr renumbered, and
        # the same terminals now sit in "w1:t1".
        self.assertTrue(self.archive_now("w1:t5", terminals=["term_w1:p1"], confirmed=True))
        self.assertIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)

    def test_archive_now_refuses_when_no_tab_has_the_terminals(self):
        with self.assertRaisesRegex(archive.Skip, "tab changed"):
            self.archive_now("w1:t1", terminals=["term_gone"], confirmed=True)
        self.assertNotIn("tab.close", self.fake.methods())

    def test_preview(self):
        result = sweep.preview(Client(self.fake.path), self.state, agents.table(), "w1:t1", now=T0)
        self.assertEqual(result, {"tab_id": "w1:t1", "label": "old", "terminals": ["term_w1:p1"], "blocks": [],
                                  "warnings": [], "activity": "Last activity 23 days ago."})
        self.assertNotIn("tab.close", self.fake.methods())

    def test_preview_of_the_focused_working_tab_only_warns(self):
        self.panes[1]["agent_status"] = "working"
        result = sweep.preview(Client(self.fake.path), self.state, agents.table(), "w1:t2", now=T0)
        self.assertEqual((result["blocks"], result["warnings"]),
                         ([], ["A pane is still working; archiving stops it."]))

    def test_preview_of_a_missing_tab(self):
        with self.assertRaisesRegex(archive.Skip, "no tab w1:t404"):
            sweep.preview(Client(self.fake.path), self.state, agents.table(), "w1:t404", now=T0)
````

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest tests.test_sweep -v`

Expected: `FAILED (errors=8)` (Ran 67 tests). Failing: `test_archive_now_archives_the_focused_tab`, `test_archive_now_confirmed_archives_through_warnings`, `test_archive_now_confirmed_still_refuses_a_block`, `test_archive_now_finds_the_tab_by_terminals_after_its_id_shifts`, .... Reason: `TypeError: archive_now() got an unexpected keyword argument 'confirmed'`; `TypeError: archive_now() got an unexpected keyword argument 'terminals'`.

- [ ] **Step 3: Implement**

In `shelf/sweep.py`, replace:

````python
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
````

with:

````python
def _snapshot(client, state: Path, now: datetime):
    """gather(), plus the activity lookup a manual archive is judged by."""
    tabs = gather(client)
    installed_at = _installed_at(state, now)
    store = activity.ActivityStore(state)
    store.update(lambda d: _record_presence(d, tabs, now))
    return tabs, _activity_lookup(store.load(), installed_at)


def _match(tabs: list, tab_id: str, terminals):
    """The (tab, panes) asked for: by its set of terminal ids when given
    (they survive herdr's positional ids shifting), otherwise by tab_id."""
    if terminals:
        found = _find(tabs, frozenset(terminals))
        if found is None:
            raise archive.Skip("tab changed")
        return found
    found = next(((t, p) for t, p in tabs if t["tab_id"] == tab_id), None)
    if found is None:
        raise archive.Skip(f"no tab {tab_id}")
    return found


def preview(client, state_dir, table: dict, tab_id: str, now: datetime | None = None) -> dict:
    """What the archive-tab popup shows for one tab: its label and terminal
    ids, and assess()'s blocks, warnings and activity line."""
    now = now or utc_now()
    tabs, activity_of = _snapshot(client, Path(state_dir), now)
    tab, panes = _match(tabs, tab_id, None)
    blocks, warnings, activity_line = assess(tab, panes, table, activity_of, now, _open_sessions(tabs))
    return {"tab_id": tab["tab_id"], "label": _display_label(tab, _workspace_labels(client)),
            "terminals": sorted(archive.pane_terminals(panes)), "blocks": blocks, "warnings": warnings,
            "activity": activity_line}


def archive_now(client, cfg: dict, state_dir, table: dict, tab_id: str, now: datetime | None = None,
                herdr_session: str = "default", terminals=None, confirmed: bool = False) -> str:
    """Archive one tab immediately, ignoring idle_days and mode.

    terminals, when given, finds the tab by its terminal ids instead of
    tab_id (see _match). confirmed=True is the archive-tab popup, whose user
    has already seen assess()'s warnings: only its blocks refuse. Otherwise
    decide() refuses as for a sweep, except that the focused tab is allowed:
    an explicit request is usually made from the tab it names.
    """
    now = now or utc_now()
    state = Path(state_dir)
    with FileLock(state / "sweep.lock", wait_seconds=ARCHIVE_NOW_LOCK_WAIT_SECONDS):
        tabs, activity_of = _snapshot(client, state, now)
        tab, panes = _match(tabs, tab_id, terminals)
        open_in = _open_sessions(tabs)
        if confirmed:
            blocks = assess(tab, panes, table, activity_of, now, open_in)[0]
            reason = blocks[0] if blocks else None
        else:
            reason = decide({**tab, "focused": False}, panes, table, activity_of, timedelta(0), now)
        if reason:
            raise archive.Skip(reason)
        _warn_if_open_elsewhere(tab["tab_id"], panes, open_in)
        return archive.archive_tab(client, archive.Archive(state), tab, panes, table, activity_of,
                                   cfg["keep_transcripts"], now, herdr_session)
````

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_sweep -v`

Expected: `Ran 67 tests`, `OK`.

Then the whole suite. Run: `python3 -m unittest discover -s tests -t .` (and `/usr/bin/python3.9 -m unittest discover -s tests -t .` if it exists)

Expected: `Ran 471 tests`, `OK` on 3.12; `OK (skipped=1)` on 3.9 (the manifest test needs 3.11+).

- [ ] **Step 5: Commit**

```bash
git add shelf/sweep.py tests/test_sweep.py
git commit -m "feat: archive_now by terminal ids, with a confirmed mode"
```

### Task 3: `shelf/confirm.py`: the popup's text and one-key read

**Files:**
- Create: `shelf/confirm.py`
- Test: `tests/test_confirm.py` (new)

`lines()` wraps every line to 60 columns so the caller can size the popup by counting lines. `read_key()` reads one byte in raw mode. `tty.setraw` flushes pending input, so a key typed before the question appeared is dropped: the tests type ahead on purpose and check that.

- [ ] **Step 1: Write the failing test**

Create `tests/test_confirm.py`:

````python
import os
import pty
import termios
import threading
import time
import unittest

from shelf import confirm

PREVIEW = {"tab_id": "w1:t1", "label": "api-refactor", "terminals": ["t1"], "blocks": [], "warnings": [],
           "activity": "Last activity 3 days ago."}


class LinesTest(unittest.TestCase):
    def test_clean_tab(self):
        self.assertEqual(confirm.lines(PREVIEW), [
            ' Archive "api-refactor"?',
            " Last activity 3 days ago.",
            " The tab closes; the restore picker brings it back.",
            " y archive   any other key cancel",
        ])

    def test_warnings_come_between_the_activity_line_and_the_hint(self):
        preview = dict(PREVIEW, warnings=["A pane is still working; archiving stops it."])
        self.assertEqual(confirm.lines(preview)[2], " A pane is still working; archiving stops it.")
        self.assertEqual(len(confirm.lines(preview)), 5)

    def test_a_long_warning_wraps_within_the_popup(self):
        warning = "Conversation 12345678 is also open in another tab; restore waits until that copy is closed."
        out = confirm.lines(dict(PREVIEW, warnings=[warning]))
        self.assertEqual(len(out), 6)
        self.assertTrue(all(len(line) <= confirm.WIDTH + 1 for line in out))


class ReadKeyTest(unittest.TestCase):
    def read_key_while_typing(self, before, after):
        """read_key() on a pty, with `before` typed ahead and `after` typed
        once it is waiting."""
        parent, child = pty.openpty()
        self.addCleanup(os.close, parent)
        self.addCleanup(os.close, child)
        mode = termios.tcgetattr(child)
        if before:
            os.write(parent, before)
        result = []
        reader = threading.Thread(target=lambda: result.append(confirm.read_key(child)), daemon=True)
        reader.start()
        time.sleep(0.2)
        os.write(parent, after)
        reader.join(2)
        self.assertFalse(reader.is_alive(), "read_key did not return")
        self.assertEqual(termios.tcgetattr(child), mode)
        return result[0]

    def test_reads_one_byte_and_restores_the_terminal_mode(self):
        self.assertEqual(self.read_key_while_typing(b"", b"yz"), b"y")

    def test_escape_is_read_as_its_own_byte(self):
        self.assertEqual(self.read_key_while_typing(b"", b"\x1b[B"), b"\x1b")

    def test_a_key_typed_before_the_question_is_dropped(self):
        self.assertEqual(self.read_key_while_typing(b"y", b"n"), b"n")


if __name__ == "__main__":
    unittest.main()
````

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest tests.test_confirm -v`

Expected: `FAILED (errors=1)` (Ran 1 test). Failing: `test_confirm`. Reason: `ImportError: Failed to import test module: test_confirm`; `ImportError: cannot import name 'confirm' from 'shelf' (shelf/__init__.py)`.

- [ ] **Step 3: Implement**

Create `shelf/confirm.py`:

````python
"""The archive-tab popup: the question it shows, and reading one key."""

from __future__ import annotations

import os
import termios
import textwrap
import tty

# Text columns: the manifest's popup width (64) less the border, the leading
# space and one spare column.
WIDTH = 60
HINT = "The tab closes; the restore picker brings it back."
KEYS = "y archive   any other key cancel"


def lines(preview: dict) -> list:
    """The popup's lines for a sweep.preview() result, wrapped to WIDTH."""
    text = [f'Archive "{preview["label"]}"?', preview["activity"], *preview["warnings"], HINT, KEYS]
    return [" " + piece for line in text for piece in textwrap.wrap(line, WIDTH)]


def read_key(fd: int) -> bytes:
    """One byte from the terminal in raw mode (b"" at end of input); the
    terminal's previous mode is restored on every path.

    tty.setraw flushes input first (TCSAFLUSH), so a key typed before the
    question was drawn is dropped rather than taken as the answer.
    """
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return os.read(fd, 1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
````

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_confirm -v`

Expected: `Ran 6 tests`, `OK`.

Then the whole suite. Run: `python3 -m unittest discover -s tests -t .` (and `/usr/bin/python3.9 -m unittest discover -s tests -t .` if it exists)

Expected: `Ran 477 tests`, `OK` on 3.12; `OK (skipped=1)` on 3.9 (the manifest test needs 3.11+).

- [ ] **Step 5: Commit**

```bash
git add shelf/confirm.py tests/test_confirm.py
git commit -m "feat: confirm popup text and one-key read"
```

### Task 4: `open-archive` and `confirm-archive` commands

**Files:**
- Modify: `shelf/__main__.py`
- Test: `tests/test_main.py` (new `ArchiveTabTest`; one test in `SessionAllowlistTest`; `open-archive` added to `test_hooks_exit_zero_without_herdr`)

`open-archive` runs from the action: it previews the tab, notifies on a block, otherwise opens the `archive-confirm` popup with the question and the tab's identity in its env (herdr's `plugin.pane.open` accepts `env` and an integer `height`; checked on herdr 0.9.1). It is a hook (`ALWAYS_HOOKS`), so it always exits 0. `confirm-archive` runs inside the popup; every outcome is a notification because the popup closes when it returns. `_logs_off_stderr()` is the stderr-handler juggling `_pick` already did, now shared.

- [ ] **Step 1: Write the failing test**

In `tests/test_main.py`, replace:

````python
from datetime import datetime, timezone
````

with:

````python
from datetime import datetime, timedelta, timezone
````

In `tests/test_main.py`, replace:

````python
        for argv in (["track"], ["sweep", "--if-due"], ["open-picker"]):
````

with:

````python
        for argv in (["track"], ["sweep", "--if-due"], ["open-picker"], ["open-archive"]):
````

In `tests/test_main.py`, insert this immediately before the line `class SessionAllowlistTest(unittest.TestCase):` (keep the blank lines shown):

````python
def _pane(pane_id, tab, session, status="idle"):
    return {"pane_id": pane_id, "tab_id": tab, "terminal_id": "term_" + pane_id, "cwd": "/src",
            "agent": "claude", "agent_status": status,
            "agent_session": {"agent": "claude", "kind": "id", "value": session, "source": "herdr:claude"}}


class ArchiveTabTest(unittest.TestCase):
    """open-archive (the archive-tab action) and confirm-archive (its popup)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeHerdr()
        self.addCleanup(self.fake.close)
        env = mock.patch.dict(os.environ, {
            "HERDR_PLUGIN_STATE_DIR": self.tmp.name,
            "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "xdg-config"),
            "XDG_STATE_HOME": os.path.join(self.tmp.name, "xdg-state"),
            "CLAUDE_CONFIG_DIR": os.path.join(self.tmp.name, "claude"),
            "CODEX_HOME": os.path.join(self.tmp.name, "codex"),
            "HERDR_SOCKET_PATH": self.fake.path,
            "HERDR_PLUGIN_ID": "shelf",
            "HERDR_TAB_ID": "w1:t1",
        })
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
        self.addCleanup(MainTest._reset_shelf_logger)
        self.session = Path(self.tmp.name) / "sessions" / "default"
        self.tabs = [{"tab_id": "w1:t1", "workspace_id": "w1", "label": "old", "focused": True}]
        self.panes = [_pane("w1:p1", "w1:t1", "OLD")]
        self.fake.handlers.update({
            "tab.list": lambda p: {"tabs": [dict(t) for t in self.tabs]},
            "pane.list": lambda p: {"panes": [dict(x) for x in self.panes]},
            "workspace.list": lambda p: {"workspaces": [{"workspace_id": "w1", "label": "main"}]},
            "notification.show": lambda p: {"type": "ok"},
            "plugin.pane.open": lambda p: {"type": "ok"},
            "layout.export": self.layout_export,
            "pane.process_info": lambda p: {"process_info": {"foreground_processes": [{"name": "claude",
                                                                                      "argv": ["claude"]}]}},
            "tab.close": self.close_tab,
        })
        (self.session / "installed_at").parent.mkdir(parents=True)
        (self.session / "installed_at").write_text("2026-01-01T00:00:00Z\n")
        (self.session / "activity.json").write_text(json.dumps(
            {"claude:OLD": {"first_seen": "2026-01-01T00:00:00Z",
                            "last_active": iso(now() - timedelta(days=3, hours=1))}}))

    def layout_export(self, p):
        pane_id = next(x["pane_id"] for x in self.panes if x["tab_id"] == p["tab_id"])
        return {"layout": {"workspace_id": "w1", "tab_id": p["tab_id"], "zoomed": False,
                           "focused_pane_id": pane_id, "root": {"type": "pane", "pane_id": pane_id, "cwd": "/src"}}}

    def close_tab(self, p):
        self.tabs = [t for t in self.tabs if t["tab_id"] != p["tab_id"]]
        self.panes = [x for x in self.panes if x["tab_id"] != p["tab_id"]]
        return {"type": "ok"}

    def notifications(self):
        return [params["body"] for method, params in self.fake.calls if method == "notification.show"]

    def opened(self):
        return [params for method, params in self.fake.calls if method == "plugin.pane.open"]

    def run_main(self, argv):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            code = main(argv)
        self.assertEqual(code, 0)
        return out.getvalue()

    # -- open-archive --

    def test_open_archive_opens_the_popup_with_the_question(self):
        self.run_main(["open-archive"])
        text = "\n".join([' Archive "old"?', " Last activity 3 days ago.",
                          " The tab closes; the restore picker brings it back.", " y archive   any other key cancel"])
        self.assertEqual(self.opened(), [{
            "plugin_id": "shelf", "entrypoint": "archive-confirm", "height": 7,
            "env": {"SHELF_TAB_ID": "w1:t1", "SHELF_TAB_LABEL": "old", "SHELF_TAB_TERMINALS": "term_w1:p1",
                    "SHELF_CONFIRM_TEXT": text}}])
        self.assertEqual(self.notifications(), [])

    def test_open_archive_shows_warnings_in_the_popup(self):
        self.panes[0]["agent_status"] = "working"
        self.run_main(["open-archive"])
        (opened,) = self.opened()
        self.assertIn(" A pane is still working; archiving stops it.", opened["env"]["SHELF_CONFIRM_TEXT"])
        self.assertEqual(opened["height"], 8)

    def test_open_archive_notifies_a_block_and_opens_nothing(self):
        self.panes[0]["agent_session"]["value"] = "-rf"
        self.run_main(["open-archive"])
        self.assertEqual(self.opened(), [])
        self.assertEqual(self.notifications(), ['shelf: can\'t archive "old": w1:p1: invalid session id'])

    def test_open_archive_without_a_tab_id(self):
        os.environ.pop("HERDR_TAB_ID")
        self.run_main(["open-archive"])
        self.assertEqual(self.notifications(), ["shelf: no tab to archive"])

    def test_open_archive_for_a_tab_that_is_gone(self):
        os.environ["HERDR_TAB_ID"] = "w1:t404"
        self.run_main(["open-archive"])
        self.assertEqual(self.notifications(), ["shelf: can't archive this tab: no tab w1:t404"])

    def test_open_archive_when_a_popup_is_already_open(self):
        def busy(_params):
            raise FakeError("ui_busy", "busy")

        self.fake.handlers["plugin.pane.open"] = busy
        self.run_main(["open-archive"])
        self.assertEqual(self.notifications(), ["shelf: close the open popup or dialog first"])

    # -- confirm-archive --

    def confirm(self, key, tab_id="w1:t1", terminals="term_w1:p1"):
        os.environ.update({"SHELF_TAB_ID": tab_id, "SHELF_TAB_LABEL": "old", "SHELF_TAB_TERMINALS": terminals,
                           "SHELF_CONFIRM_TEXT": ' Archive "old"?'})
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in
                                 ("SHELF_TAB_ID", "SHELF_TAB_LABEL", "SHELF_TAB_TERMINALS", "SHELF_CONFIRM_TEXT")])
        with mock.patch("shelf.__main__.confirm.read_key", return_value=key):
            return self.run_main(["confirm-archive"])

    def test_confirm_archive_y_archives(self):
        out = self.confirm(b"y")
        self.assertIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)
        self.assertEqual(self.notifications(), ['shelf: archived "old"'])
        self.assertEqual(out, ' Archive "old"?\n Archiving...\n')

    def test_confirm_archive_capital_y_archives(self):
        self.confirm(b"Y")
        self.assertIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)

    def test_confirm_archive_archives_through_warnings(self):
        self.panes[0]["agent_status"] = "working"
        self.confirm(b"y")
        self.assertIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)

    def test_confirm_archive_any_other_key_cancels(self):
        for key in (b"n", b"\x1b", b"\x03", b""):
            with self.subTest(key=key):
                self.fake.calls.clear()
                out = self.confirm(key)
                self.assertNotIn("tab.close", self.fake.methods())
                self.assertEqual(self.notifications(), [])
                self.assertNotIn("Archiving", out)

    def test_confirm_archive_finds_the_tab_after_its_id_shifts(self):
        self.confirm(b"y", tab_id="w1:t7")
        self.assertIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)

    def test_confirm_archive_when_the_tab_is_gone(self):
        self.confirm(b"y", terminals="term_gone")
        self.assertNotIn("tab.close", self.fake.methods())
        self.assertEqual(self.notifications(), ['shelf: can\'t archive "old": tab changed'])

    def test_confirm_archive_notifies_a_block(self):
        self.panes[0]["agent_session"]["value"] = "-rf"
        self.confirm(b"y")
        self.assertNotIn("tab.close", self.fake.methods())
        self.assertEqual(self.notifications(), ['shelf: can\'t archive "old": w1:p1: invalid session id'])

    def test_confirm_archive_while_a_sweep_holds_the_lock(self):
        with mock.patch("shelf.sweep.ARCHIVE_NOW_LOCK_WAIT_SECONDS", 0.1), FileLock(self.session / "sweep.lock"):
            self.confirm(b"y")
        self.assertNotIn("tab.close", self.fake.methods())
        self.assertEqual(self.notifications(), ["shelf: a sweep is running; try again in a moment"])
````

In `tests/test_main.py`, insert this immediately before the line `# -- Manual commands: a disabled session prints a message and exits 1. --` (keep the blank lines shown):

````python
    def test_disabled_open_and_confirm_archive_show_a_notification_and_archive_nothing(self):
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        fake.handlers["notification.show"] = lambda p: {"type": "ok"}
        self.use_cao_socket_linked_to(fake)
        for argv in (["open-archive"], ["confirm-archive"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                self.assertEqual(main(argv), 0)
        self.assertEqual(fake.methods(), ["notification.show", "notification.show"])
        self.assertFalse(self.cao_session.exists())
````

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest tests.test_main -v`

Expected: `FAILED (failures=10, errors=11)` (Ran 78 tests). Failing: `test_confirm_archive_any_other_key_cancels`, `test_confirm_archive_archives_through_warnings`, `test_confirm_archive_capital_y_archives`, `test_confirm_archive_finds_the_tab_after_its_id_shifts`, .... Reason: `AttributeError: module 'shelf.__main__' has no attribute 'confirm'. Did you mean: 'config'?`; `AssertionError: 2 != 0`.

- [ ] **Step 3: Implement**

In `shelf/__main__.py`, replace:

````python
import time
from pathlib import Path
````

with:

````python
import time
from contextlib import contextmanager
from pathlib import Path
````

In `shelf/__main__.py`, replace:

````python
from . import activity, agents, archive, config, migrate, picker, restore, session, sweep
````

with:

````python
from . import activity, agents, archive, config, confirm, migrate, picker, restore, session, sweep
````

In `shelf/__main__.py`, replace:

````python
ALWAYS_HOOKS = ("track", "open-picker")
````

with:

````python
ALWAYS_HOOKS = ("track", "open-picker", "open-archive")
````

In `shelf/__main__.py`, replace:

````python
USAGE = ("usage: python3 -m shelf {track | sweep [--if-due] | archive <tab-id> | open-picker | pick | "
         "list | restore <archive-id>}")
````

with:

````python
USAGE = ("usage: python3 -m shelf {track | sweep [--if-due] | archive <tab-id> | open-archive | "
         "confirm-archive | open-picker | pick | list | restore <archive-id>}")
````

In `shelf/__main__.py`, insert this immediately before the line `def _notify_disabled(client, session_name) -> None:` (keep the blank lines shown):

````python
def _notify(client, body: str) -> None:
    try:
        client.call("notification.show", {"title": "shelf", "body": body})
    except HerdrError as e:
        log.warning("notification failed: %s", e)
````

In `shelf/__main__.py`, replace:

````python
        # The popup's terminal belongs to curses while it runs: a log line on
        # stderr (a restore logs several) would draw over it. shelf.log
        # still gets every line.
        on_stderr = [h for h in log.handlers if type(h) is logging.StreamHandler]
        for handler in on_stderr:
            log.removeHandler(handler)
        quiet = logging.NullHandler()  # with no shelf.log, logging.lastResort would write to stderr
        log.addHandler(quiet)
        try:
            picker.run(arch, do_restore, now, notify=notify)
        finally:
            log.removeHandler(quiet)
            for handler in on_stderr:
                log.addHandler(handler)
````

with:

````python
        with _logs_off_stderr():
            picker.run(arch, do_restore, now, notify=notify)
````

In `shelf/__main__.py`, insert this immediately before the line `def _pick(state: Path) -> int:` (keep the blank lines shown):

````python
@contextmanager
def _logs_off_stderr():
    """Keep log lines off a popup's screen while it runs: a line on stderr
    (a restore or an archive logs several) would draw over it. shelf.log
    still gets every line."""
    on_stderr = [h for h in log.handlers if type(h) is logging.StreamHandler]
    for handler in on_stderr:
        log.removeHandler(handler)
    quiet = logging.NullHandler()  # with no shelf.log, logging.lastResort would write to stderr
    log.addHandler(quiet)
    try:
        yield
    finally:
        log.removeHandler(quiet)
        for handler in on_stderr:
            log.addHandler(handler)


def _open_archive(client, session_state: Path) -> None:
    """Ask before archiving the tab herdr invoked this action for
    (HERDR_TAB_ID): a notification for a block, otherwise the popup."""
    tab_id = os.environ.get("HERDR_TAB_ID")
    if not tab_id:
        _notify(client, "shelf: no tab to archive")
        return
    cfg = config.load(_config_dir())
    found = sweep.preview(client, session_state, agents.table(cfg["agents"]), tab_id)
    if found["blocks"]:
        _notify(client, f'shelf: can\'t archive "{found["label"]}": {found["blocks"][0]}')
        return
    lines = confirm.lines(found)
    try:
        client.call("plugin.pane.open", {
            "plugin_id": os.environ.get("HERDR_PLUGIN_ID") or PLUGIN_ID, "entrypoint": "archive-confirm",
            "height": len(lines) + 3,  # the border, and a spare row for "Archiving..."
            "env": {"SHELF_TAB_ID": found["tab_id"], "SHELF_TAB_LABEL": found["label"],
                    "SHELF_TAB_TERMINALS": ",".join(found["terminals"]), "SHELF_CONFIRM_TEXT": "\n".join(lines)}})
    except HerdrError as e:
        if e.code != "ui_busy":
            raise
        _notify(client, "shelf: close the open popup or dialog first")


def _confirm_archive(client, session_state: Path, session_name: str) -> None:
    """The archive-confirm popup's own command: show the question, read one
    key, and archive on y. Every outcome is a notification, since the popup
    closes as soon as this returns."""
    label = os.environ.get("SHELF_TAB_LABEL") or "tab"
    print(os.environ.get("SHELF_CONFIRM_TEXT", ""), flush=True)
    if confirm.read_key(sys.stdin.fileno()) not in (b"y", b"Y"):
        return
    print(" Archiving...", flush=True)
    terminals = [t for t in os.environ.get("SHELF_TAB_TERMINALS", "").split(",") if t]
    try:
        with _logs_off_stderr():
            cfg = config.load(_config_dir())
            sweep.archive_now(client, cfg, session_state, agents.table(cfg["agents"]),
                              os.environ.get("SHELF_TAB_ID", ""), herdr_session=session_name,
                              terminals=terminals, confirmed=True)
    except LockBusy:
        _notify(client, "shelf: a sweep is running; try again in a moment")
    except Exception as e:
        log.exception("confirm-archive failed")
        _notify(client, f'shelf: can\'t archive "{label}": {e}')
    else:
        _notify(client, f'shelf: archived "{label}"')
````

In `shelf/__main__.py`, insert this immediately before the line `if command in ("archive", "restore") and not args:` (keep the blank lines shown):

````python
    if command == "open-archive":
        client = Client()
        if not allowed():
            _notify_disabled(client, session_name)
            return 0
        if _migration_incomplete(state):
            _notify(client, _MIGRATING_MESSAGE)
            return 0
        try:
            _open_archive(client, session_state)
        except Exception as e:
            log.exception("open-archive failed")
            _notify(client, f"shelf: can't archive this tab: {e}")
        return 0
    if command == "confirm-archive":
        client = Client()
        if not allowed():
            _notify_disabled(client, session_name)
            return 0
        if _migration_incomplete(state):
            _notify(client, _MIGRATING_MESSAGE)
            return 0
        _confirm_archive(client, session_state, session_name)
        return 0
````

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_main -v`

Expected: `Ran 78 tests`, `OK`.

Then the whole suite. Run: `python3 -m unittest discover -s tests -t .` (and `/usr/bin/python3.9 -m unittest discover -s tests -t .` if it exists)

Expected: `Ran 492 tests`, `OK` on 3.12; `OK (skipped=1)` on 3.9 (the manifest test needs 3.11+).

- [ ] **Step 5: Commit**

```bash
git add shelf/__main__.py tests/test_main.py
git commit -m "feat: open-archive and confirm-archive commands"
```

### Task 5: Manifest: the action and the popup

**Files:**
- Modify: `herdr-plugin.toml`
- Test: `tests/test_manifest.py`

`contexts = ["tab"]` has no visible effect in herdr today (its right-click menus are fixed), but it is the honest declaration, and the command reads `HERDR_TAB_ID` rather than assuming focus. This test needs Python 3.11+ (`tomllib`); on 3.9 it is skipped, so run it with a newer `python3`.

- [ ] **Step 1: Write the failing test**

In `tests/test_manifest.py`, replace:

````python
        self.assertEqual({a["id"] for a in m["actions"]}, {"restore", "sweep-now"})
````

with:

````python
        self.assertEqual({a["id"] for a in m["actions"]}, {"restore", "archive-tab", "sweep-now"})
        archive_tab = next(a for a in m["actions"] if a["id"] == "archive-tab")
        self.assertEqual(archive_tab["contexts"], ["tab"])
        self.assertEqual(archive_tab["command"], ["python3", "-m", "shelf", "open-archive"])
````

In `tests/test_manifest.py`, insert this immediately after the line `self.assertEqual((m["panes"][0]["width"], m["panes"][0]["height"]), ("80%", 18))` (keep the blank lines shown):

````python
        confirm = m["panes"][1]
        self.assertEqual((confirm["id"], confirm["placement"], confirm["width"], confirm["height"]),
                         ("archive-confirm", "popup", 64, 8))
        self.assertEqual(confirm["command"], ["python3", "-m", "shelf", "confirm-archive"])
````

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest tests.test_manifest -v`

Expected: `FAILED (failures=1)` (Ran 1 test). Failing: `test_manifest`. Reason: `AssertionError: Items in the second set but not the first:`.

- [ ] **Step 3: Implement**

In `herdr-plugin.toml`, insert this immediately before the line `[[actions]]` (keep the blank lines shown):

````toml
[[actions]]
id = "archive-tab"
title = "Shelf: archive this tab"
contexts = ["tab"]
command = ["python3", "-m", "shelf", "open-archive"]
````

In `herdr-plugin.toml`, insert this immediately after the line `command = ["python3", "-m", "shelf", "pick"]` (keep the blank lines shown):

````toml
[[panes]]
id = "archive-confirm"
title = "Shelf"
placement = "popup"
width = 64
height = 8
command = ["python3", "-m", "shelf", "confirm-archive"]
````

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_manifest -v`

Expected: `Ran 1 test`, `OK`.

Then the whole suite. Run: `python3 -m unittest discover -s tests -t .` (and `/usr/bin/python3.9 -m unittest discover -s tests -t .` if it exists)

Expected: `Ran 492 tests`, `OK` on 3.12; `OK (skipped=1)` on 3.9 (the manifest test needs 3.11+).

- [ ] **Step 5: Commit**

```bash
git add herdr-plugin.toml tests/test_manifest.py
git commit -m "feat: archive-tab action and confirm popup in the manifest"
```

### Task 6: Docs and version 0.5.0

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `AGENTS.md`, `shelf/__init__.py`, `herdr-plugin.toml`

The README gets the binding next to the restore one in Install, an "Archive a tab now" section, and a note on `archive <tab-id>`. The popup's hint names the restore picker, not a key, because each user binds their own. `tests/test_manifest.py` checks that the two version strings match.

- [ ] **Step 1: Edit the docs and the version**

In `README.md`, replace:

````markdown
there.

## It starts in dry-run
````

with:

````markdown
there.

To also archive the current tab on demand (see
[Archive a tab now](#archive-a-tab-now)), add a second key; `prefix+shift+a`
is unbound in the default keymap too:

```toml
[[keys.command]]
key = "prefix+shift+a"
type = "plugin_action"
command = "shelf.archive-tab"
description = "archive the current tab"
```

## It starts in dry-run
````

In `README.md`, insert this immediately before the line `## Supported agents` (keep the blank lines shown):

````markdown
## Archive a tab now

Press your archive key (above) in the tab you are done with. Shelf asks
first:

```
 Archive "api-refactor"?
 Last activity 3 days ago.
 The tab closes; the restore picker brings it back.
 y archive   any other key cancel
```

`y` archives and closes the tab; any other key, including Esc, cancels.
Unlike a sweep, this ignores `idle_days` and dry-run mode, and archives the
tab you are looking at. The question also warns about anything unusual, and
`y` still archives:

- a pane is still working (archiving stops it);
- the tab has no agent, or a pane has no session id or runs an agent Shelf
  cannot resume (that pane comes back as a shell);
- the conversation is open in two panes here (both come back resuming it),
  or in another tab (restore waits until that copy is closed).

It refuses, with a notification instead of the question, only when the entry
could not be restored correctly: a pane carrying another agent's session, or
an invalid session id.
````

In `README.md`, replace:

````markdown
Tab ids for `archive` come from `herdr tab list`.
````

with:

````markdown
Tab ids for `archive` come from `herdr tab list`. It archives the focused
tab too, but with no question to answer it still refuses what the archive
key only warns about (a working pane, a missing session id, and so on).
````

In `CHANGELOG.md`, replace:

````markdown
# Changelog

## 0.4.0 - 2026-09-30
````

with:

````markdown
# Changelog

## 0.5.0 - unreleased

- Add the `shelf.archive-tab` action: bound to a key, it asks whether to
  archive the current tab, showing when it was last active and warning about
  anything unusual (a working pane, a pane that would come back as a shell,
  a conversation open elsewhere), and archives it on `y`. It refuses only
  when the entry could not be restored correctly. herdr cannot add plugin
  entries to its right-click menus, so a key is the way in; see the README
  for the binding.
- `python3 -m shelf archive <tab-id>` now archives the focused tab too.

## 0.4.0 - 2026-09-30
````

In `AGENTS.md`, replace:

````markdown
- `shelf/__main__.py`: CLI entry point (`track`, `sweep [--if-due]`,
  `archive <tab-id>`, `open-picker`, `pick`, `list`, `restore <id>`). Runs the
  migration first, then gates every command on the herdr session allowlist.
````

with:

````markdown
- `shelf/__main__.py`: CLI entry point (`track`, `sweep [--if-due]`,
  `archive <tab-id>`, `open-archive`, `confirm-archive`, `open-picker`,
  `pick`, `list`, `restore <id>`). Runs the migration first, then gates every
  command on the herdr session allowlist.
````

In `AGENTS.md`, replace:

````markdown
- `shelf/sweep.py`: eligibility (`decide()`), gathering tabs/panes, dry-run
  reporting vs. live archiving, one summary notification per sweep.
````

with:

````markdown
- `shelf/sweep.py`: eligibility (`decide()` for sweeps; `assess()` for the
  archive-tab popup's blocks and warnings), gathering tabs/panes, dry-run
  reporting vs. live archiving, one summary notification per sweep;
  `archive_now()` and `preview()` for archiving one tab on request.
````

In `AGENTS.md`, insert this immediately before the line `- `shelf/session.py`:` (keep the blank lines shown):

````markdown
- `shelf/confirm.py`: the archive-tab popup's text (`lines()`) and its
  one-key read in raw tty mode (`read_key()`).
````

In `shelf/__init__.py`, replace:

````python
__version__ = "0.4.0"
````

with:

````python
__version__ = "0.5.0"
````

In `herdr-plugin.toml`, replace:

````toml
version = "0.4.0"
````

with:

````toml
version = "0.5.0"
````

- [ ] **Step 2: Run the suite**

Run: `python3 -m unittest discover -s tests -t .` (and `/usr/bin/python3.9 -m unittest discover -s tests -t .` if it exists)

Expected: `Ran 492 tests`, `OK` on 3.12; `OK (skipped=1)` on 3.9 (the manifest test needs 3.11+).

- [ ] **Step 3: Commit**

```bash
git add README.md CHANGELOG.md AGENTS.md shelf/__init__.py herdr-plugin.toml
git commit -m "docs: archive a tab now; version 0.5.0"
```

### Task 7: Final check and hand-off

- [ ] **Step 1: Whole suite on both Pythons**

Run: `python3 -m unittest discover -s tests -t .` (and `/usr/bin/python3.9 -m unittest discover -s tests -t .` if it exists)

Expected: `Ran 492 tests`, `OK` on 3.12; `OK (skipped=1)` on 3.9 (the manifest test needs 3.11+).

- [ ] **Step 2: Nothing stray**

Run: `git status --short` and `git log --oneline main..`

Expected: a clean tree, and the six commits from Tasks 1-6.

- [ ] **Step 3: Finish the branch**

Use superpowers:finishing-a-development-branch. Merging squashes the branch into one commit on `main`. Don't push, tag or release without the user's go-ahead (Task 8).

### Task 8: Release and roll out (only when the user asks)

- [ ] **Step 1: Release**

In `CHANGELOG.md`, change `## 0.5.0 - unreleased` to `## 0.5.0 - <today, YYYY-MM-DD>`, then:

```bash
git add CHANGELOG.md
git commit -m "chore: release 0.5.0"
git push origin main
git tag v0.5.0 && git push origin v0.5.0
gh release create v0.5.0 --title v0.5.0 --notes "$(sed -n '/^## 0.5.0/,/^## 0.4.0/{/^## /d;p}' CHANGELOG.md)"
gh run list --limit 3
```

Expected: the CI run for the release commit passes on every OS and Python in the matrix.

- [ ] **Step 2: Install on each machine that runs Shelf**

```bash
cp -a ~/.local/state/herdr/plugins/shelf ~/.local/state/herdr/plugins/shelf.bak-pre-0.5.0
herdr plugin install anilkmr-a2z/herdr-shelf --ref v0.5.0 --yes
herdr plugin action list | grep -o '"action_id":"archive-tab"'
```

Expected: `"action_id":"archive-tab"`.

- [ ] **Step 3: Bind the key**

Add to `~/.config/herdr/config.toml`, after the `shelf.restore` binding, then run `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+shift+a"
type = "plugin_action"
command = "shelf.archive-tab"
description = "archive the current tab"
```

`config.toml` is per machine; repeat this on every machine that runs 0.5.0, and not before it does (until then the binding names an action that doesn't exist).

- [ ] **Step 4: Live check (needs a person at the keyboard)**

1. Open a throwaway tab, start `claude` in it, and send one short prompt so it has a session id. Note it: `herdr pane get <pane-id>` shows `agent_session.value`.
2. Press prefix+Shift+A in that tab. Expected: the popup shows `Archive "<label>"?`, `Last activity today.`, the restore hint and the keys.
3. Press Esc. Expected: the popup closes, and the tab is still there.
4. Press prefix+Shift+A again, then `y`. Expected: the tab closes, and a notification says `shelf: archived "<label>"`.
5. Open the restore picker and restore it. Expected: the tab comes back, and `herdr pane get` shows the same `agent_session.value`.
6. In step 4, also check that herdr doesn't freeze when the tab closes, and that the `archived` notification really appears. If it doesn't, the popup was killed before it could send it: move the work after `y` into a process detached from the popup's session.
