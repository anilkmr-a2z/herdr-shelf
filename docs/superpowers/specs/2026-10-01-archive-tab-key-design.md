# Archive the current tab with a key

Status: approved design, 2026-10-01. Target release: 0.5.0.

## Problem

A tab leaves only when a sweep finds it idle for `idle_days`, or when someone
types `python3 -m shelf archive <tab-id>` in a terminal. There is no quick way
to shelve the tab you are looking at once you are done with it.

## Why not a right-click menu item

The first idea was an "Archive" entry in herdr's tab right-click menu. herdr
offers no way to add one:

- The menus are fixed lists in herdr's `src/client/shell/context_menu.rs`; the
  tab menu is New tab, Rename, Close. This holds for 0.9.1, 0.9.3,
  and master at `d6b40d4` (2026-10-01).
- A plugin action's `contexts` field (`global`, `workspace`, `tab`, `pane`,
  `selection`) is parsed and echoed by `plugin.action.list`, and nothing else
  reads it.
- Upstream issues #1511, #1671, #1776 and #1830 asked for exactly this; each
  was closed as not planned for not using the bug template. Discussions #1672
  and #1722 have no maintainer reply.

So the action is bound to a key instead. It declares `contexts = ["tab"]`; if
herdr ever renders `contexts` in its menus, the entry would appear in the tab
menu with no change here, because the command reads the tab it was invoked
for from `HERDR_TAB_ID` rather than assuming the focused one.

## Goals

- prefix+Shift+A asks to archive the current tab, and `y` archives it.
- The question says when the tab was last active, and warns about anything
  that makes archiving it unusual, but the choice stays with the user.
- Never archive a different tab than the one that was asked about.

## Non-goals

- A right-click menu entry (see above).
- Archiving several tabs at once, or choosing a tab other than the current one.
- Shipping the keybinding inside the plugin: herdr plugins cannot declare
  keybindings, so the binding lives in each machine's `config.toml`.
- Changing what `python3 -m shelf archive <tab-id>` refuses, beyond dropping
  the focused-tab rule: with no confirmation step it keeps refusing what the
  popup only warns about (see "Command line").

## Behaviour

1. prefix+Shift+A invokes the `archive-tab` action. herdr sets `HERDR_TAB_ID`
   to the active tab of the focused workspace.
2. `open-archive` finds the tab and works out its activity line and warnings
   (see "Warnings and blocks"). Only a block stops it: then a notification
   reads `shelf: can't archive "<label>": <reason>` and no popup opens.
3. Otherwise a popup opens. Its first lines are the question and the activity
   line, then one line per warning, then the restore hint and the keys:

   ```
    Archive "api-refactor"?
    Last activity 3 days ago.
    The tab closes; the restore picker brings it back.
    y archive   any other key cancel
   ```

   With warnings:

   ```
    Archive "perf-probe"?
    Last activity today.
    A pane is still working; archiving stops it.
    Pane w1:p3 has no session id; it comes back as a shell.
    The tab closes; the restore picker brings it back.
    y archive   any other key cancel
   ```

4. `y` or `Y` archives the tab. The popup shows `Archiving...` while it waits
   for the sweep lock (up to `ARCHIVE_NOW_LOCK_WAIT_SECONDS`, 10 s), the tab
   closes, the popup closes, and a notification reads
   `shelf: archived "<label>"`.
5. Any other key, including Esc and Ctrl-C, closes the popup and changes
   nothing.

## Warnings and blocks

The activity line is always shown. It uses the most recent activity across
the tab's agent panes, counted in whole 24-hour days as the picker's idle
column is (`(now - last).days`): `Last activity today.` for 0,
`Last activity 1 day ago.`, `Last activity N days ago.` When no agent pane
has recorded activity: `No activity recorded for this tab yet.`

Warnings are shown in the popup and do not stop `y`. Each is something a
sweep refuses today, but where archiving still produces a record that
restores correctly:

| Condition | Warning line |
|---|---|
| Any pane is working (an agent, or a shell running a command) | `A pane is still working; archiving stops it.` |
| No agent pane at all | `No agent in this tab; it comes back as shells.` |
| An agent pane with no session id | `Pane <id> has no session id; it comes back as a shell.` |
| An agent not in the agent table | `Pane <id> runs <agent>, which shelf cannot resume; it comes back as a shell.` |
| The same conversation in two panes of this tab | `Conversation <first 8> is open in two panes here; both come back resuming it.` |
| The same conversation open in another tab | `Conversation <first 8> is also open in another tab; restore waits until that copy is closed.` |

The focused-tab rule is dropped entirely for this path: it keeps a sweep away
from the tab you are looking at, and the tab you press a key in is always the
focused one.

The hint names the restore picker rather than a key, since each user binds
their own.

Blocks stop the archive, because the record would restore the wrong thing or
nothing at all:

| Condition | Why |
|---|---|
| An invalid session id | `agents.relaunch_argv` refuses it, so the entry could never be restored. |
| A pane whose session belongs to a different agent than the one running | restore would resume that other agent's old conversation in its place. |
| `capture`'s own refusals (no layout, the layout changed, too many panes, too deep) | the record cannot be built. These surface after `y`. |

Conditions are re-read when `y` is pressed, but only blocks are checked
again: the user has already seen and accepted the warnings.

`archive.capture()` records an agent pane whose session has no usable id
(missing, empty, or not a dict) as a plain shell, which is what the "no
session id" warning promises. Repeated warning lines are shown once.

`archive_now` already ignores `idle_days` and `mode`, so on a machine whose
sweep is dry-run, this key still archives for real.

## Identifying the tab

herdr's tab ids are positions and compact when a tab closes, so the id read
when the key was pressed may name a different tab by the time `y` is pressed.
`open-archive` passes the popup both the tab id and the tab's set of terminal
ids (`archive.pane_terminals`). `archive_now` gains an optional `terminals`
argument; when it is given, the tab is found by that set (`sweep._find`)
instead of by id. If no tab has that set, it raises
`Skip("tab changed")`. `archive_tab` keeps its own check that the tab is
unchanged between capture and close.

## Architecture

`herdr-plugin.toml`:

```toml
[[actions]]
id = "archive-tab"
title = "Shelf: archive this tab"
contexts = ["tab"]
command = ["python3", "-m", "shelf", "open-archive"]

[[panes]]
id = "archive-confirm"
title = "Shelf"
placement = "popup"
width = 64
height = 8
command = ["python3", "-m", "shelf", "confirm-archive"]
```

`shelf/sweep.py`:

- `assess(tab, panes, table, activity_of, now, open_in)` returns
  `(blocks, warnings, activity_line)`, built from the two tables above.
  `decide` keeps its current behaviour for sweeps.
- `_snapshot(client, state, now)`: the gather and activity lookup that
  `archive_now` already does, shared with `preview`.
- `_match(tabs, tab_id, terminals)`: finds the tab by terminals when given,
  otherwise by id.
- `preview(client, state_dir, table, tab_id)`: the tab's id, display label
  (`_display_label`, as sweep reports use), sorted terminal ids, and
  `assess`'s result. It takes no sweep lock; the confirm step re-checks
  under it. Because of that, it reads `pane.list` once before the gather
  and refuses with `tabs changed while being read; try again` unless the
  gather's own `pane.list` matches it: on herdr 0.9.0, whose tab ids are
  positions, a tab closing between `tab.list` and `pane.list` would pair one
  tab's label with another tab's panes. An empty terminal list matches no
  tab, so a lost list fails closed.
- `archive_now(..., terminals=None, confirmed=False)`: with
  `confirmed=True` it refuses only on `assess`'s blocks; otherwise it keeps
  calling `decide` as today, minus the focused-tab rule.

`shelf/confirm.py` (new):

- `lines(preview)`: the popup's lines, each wrapped to 60 columns (the
  64-column popup less its border, a leading space and a spare column), so
  `open-archive` can size the popup by counting them. Non-printable
  characters show as `?`: any process can set a tab label, and an escape
  sequence printed raw could redraw the question.
- `read_key(fd)`: one byte in raw mode via `tty.setraw`, restoring the mode
  on every path. `tty.setraw` flushes pending input, so a key typed before
  the question was drawn is dropped rather than taken as the answer.

`shelf/__main__.py` gains two commands. Neither has a terminal the user
reads afterwards, so every failure reaches the user as a notification
(`notification.show`, title `shelf`), and is logged as today.

- `open-archive`:
  - A disabled session gets the existing `_notify_disabled` notification; an
    incomplete migration gets `_MIGRATING_MESSAGE`.
  - No `HERDR_TAB_ID` gets `shelf: no tab to archive`.
  - Gathers once, finds the tab, and calls `assess`; a block is the "can't
    archive" notification.
  - Opens the popup with `plugin.pane.open` (`entrypoint =
    "archive-confirm"`), `height` set to the number of `confirm.lines` plus 3
    (border, and a spare row for `Archiving...`), and `env` set to `SHELF_TAB_ID`,
    `SHELF_TAB_LABEL`, `SHELF_TAB_TERMINALS` (comma-separated) and
    `SHELF_CONFIRM_TEXT` (the popup's lines, newline-joined). `ui_busy` gets
    the existing `shelf: close the open popup or dialog first` notification.
  - Any other failure (the tab gone, an invalid `config.json`, a herdr
    error) gets `shelf: can't archive this tab: <error>`.
- `confirm-archive`:
  - Prints `SHELF_CONFIRM_TEXT` and reads one key with `confirm.read_key`.
    No curses. A failure there gets the "can't archive" notification;
    Ctrl-C or end of input before a key just cancels.
  - After `y`, ignores SIGINT and SIGHUP. herdr closes the popup, hanging
    up its terminal, once the tab it was opened over is gone, which is the
    tab being archived; without this the process would die before the
    "archived" notification. herdr follows SIGHUP with SIGTERM after 250 ms,
    so the notification still races that; the live check confirms it
    arrives, and if it doesn't, the archive moves into a process detached
    from the popup. `Archiving...` is printed without a newline, which would
    scroll the question off the popup.
  - On `y`/`Y`, calls `archive_now(..., tab_id, terminals=...,
    confirmed=True)`. A `Skip` gets the "can't archive" notification;
    `LockBusy` gets `shelf: a sweep is running; try again in a moment`;
    any other failure gets the "can't archive" notification with its error.
  - Keeps log lines off the popup for its whole run, with a
    `_logs_off_stderr` context manager factored out of `_pick`. A `Skip` is
    logged at info, without a traceback.
  - Exits 0 on every handled path, which closes the popup.

Only Python 3.9 features are used.

## Command line

`python3 -m shelf archive <tab-id>` has no confirmation step, so it keeps
refusing on everything `decide` refuses, except the focused-tab rule, which
it drops too: running it from the tab you mean to archive is the same
explicit request.

## Testing

- `tests/test_sweep.py`:
  - `assess`: the activity line for 0, 1 and N days and for no activity;
    each warning row; each block row; a tab with several warnings at once.
  - `archive_now`: archives a focused tab; with `confirmed=True` archives
    through every warning and refuses each block; `terminals` finds the tab
    after its id shifts, and raises `Skip` when no tab matches.
- `tests/test_main.py`, against the fake herdr client:
  - `open-archive`: a clean tab and a tab with warnings each open the popup
    with the right text, height and env; a block notifies and opens nothing;
    a disabled session; a missing `HERDR_TAB_ID`; `ui_busy`.
  - `confirm-archive`: `y` and `Y` archive, including a tab with warnings;
    `n`, Esc and Ctrl-C do not; `Skip` and `LockBusy` notify; the tab's id
    shifting after another tab closes still archives the right tab; the tab
    gone archives nothing.
- `tests/test_manifest.py`: the new action and pane.
- `tests/test_confirm.py`: `lines` for a clean tab, with a warning, and with
  a warning long enough to wrap; `read_key` on a pty reads one byte, reads
  Esc as its own byte, drops a key typed beforehand, and restores the mode.
- Live check in herdr: open a throwaway Claude tab, press prefix+Shift+A,
  check the activity line, cancel with Esc, press again, archive with `y`,
  then restore it from the picker with the same session id.

## Docs and release

- README: a "Archive a tab now" subsection with the key, the popup, its
  warnings, and the `config.toml` snippet; the "Command line" section's
  `archive` line notes it now accepts the focused tab.
- CHANGELOG 0.5.0: the new action and popup, and that a manual archive no
  longer refuses the focused tab.
- `herdr-plugin.toml`: version 0.5.0.

## Rollout

The binding goes in each machine's herdr `config.toml`, after 0.5.0 is
installed there; until then it names an action that does not exist:

```toml
[[keys.command]]
key = "prefix+shift+a"
type = "plugin_action"
command = "shelf.archive-tab"
description = "archive the current tab"
```
