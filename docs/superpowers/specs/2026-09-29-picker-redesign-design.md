# Picker redesign: arrow keys, archive-date groups, scrolling

Status: approved design, 2026-09-29. Target release: 0.4.0.

## Problem

The restore picker (`python3 -m shelf pick`, opened by the `restore` action)
is a numbered `input()` prompt. You type a number and press Enter to restore,
`d <number>` to delete, and Esc then Enter to close. It shows only as many
entries as fit the popup; the rest are reachable only through
`python3 -m shelf list` and `restore <id>`. Every entry is shown at once, so
a tab that just vanished in a sweep is mixed in with ones shelved months ago.

## Goals

- Move with the arrow keys and restore with Enter.
- Group entries by when they were archived, so a tab that just vanished is
  at the top, and hide entries archived 30+ days ago until asked for.
- Show 10 rows at a time and scroll to reach every entry.
- Show where a tab came from before restoring it.
- Filter by name, and support the mouse.

## Non-goals

- Configurable group boundaries. The four groups below are constants; add a
  config key only if someone needs other ones.
- Remembering which groups were open between popups.
- Number shortcuts (typing `3` to restore row 3). They are removed.
- Selecting several entries at once, or previewing conversation content.
- A fallback to the old `input()` picker.

## Screen

This is the popup as it opens, with 11 entries:

```
 Shelf - 11 archived

 - Archived today (2)
 > flaky-test         backend   codex   15d
   perf-probe         backend   claude  21d
 - Last 7 days (3)
   api-refactor       backend   claude  17d
   release-notes      docs      claude  18d
   onboarding         docs      claude  28d
 - Last 30 days (2)
   db-migration       backend   claude  34d
   style-guide        docs      claude  42d
 v 1 more
 ~/src/backend  shelved Sep 29 09:14  1 pane: codex
 Up/Down move  Enter restore  Right/Left open/close  / filter  d delete  q quit
```

The row below the window is the collapsed `+ Older (4)` header.

From the top:

1. **Title:** `Shelf - N archived`, where N is the total archive count.
2. **Up marker:** `^ N more` when rows are hidden above the window; blank otherwise.
3. **List window:** exactly 10 rows. Group headers count as rows.
4. **Down marker:** `v N more` when rows are hidden below; blank otherwise.
5. **Details line:** for the highlighted tab, the workspace directory (with
   `~` for the home directory), the local date and time it was shelved, and
   its pane count with agent names. It is blank when a group header is highlighted.
6. **Status line:** the key hints; or, when there is one, a message, a filter
   being typed, or a delete confirmation.

The highlighted row is drawn in reverse video and marked with `>`. A tab row
shows the tab label, workspace label, agent names and idle days (`17d`), each
column truncated to fit the width, as the current picker does.

The manifest's popup height changes from `"80%"` to `18` (terminal cells):
the 15 lines above plus the border and one spare. The width stays `"80%"`.
If the terminal is shorter, rows are dropped in this order: the details line,
then the markers, then list rows. Below 5 rows or 30 columns the popup shows
only `Popup too small`.

## Groups

- Four groups, by how many local calendar days ago the entry was archived
  (`archived_at`, converted from UTC to the machine's local time). With `d`
  as today's local date minus the archive's local date:
  - "Archived today": `d` is 0.
  - "Last 7 days": `d` is 1 to 6.
  - "Last 30 days": `d` is 7 to 29.
  - "Older": `d` is 30 or more, or `archived_at` is missing or unparseable.
- Calendar days, not 24-hour periods: a tab shelved at 23:50 yesterday is in
  "Last 7 days" at 00:10 today. This also keeps DST changes out of the math.
- Within a group, entries are sorted most recently archived first. Ties
  (tabs archived by the same sweep) go to the least idle first.
- Each tab row still shows its idle days.
- A header reads `- <name> (<count>)` when open and `+ <name> (<count>)` when
  collapsed.
- When the popup opens, every group is open except "Older". If "Older" is the
  only group with entries, it opens instead, so the popup never opens to a
  single collapsed row.
- A group with no entries is not shown.
- The cursor starts on the first tab row.

## Keys

| Key | Moving | Filter typing | Delete confirm |
|---|---|---|---|
| Up/Down, k/j | Move one row, headers included | - | - |
| PgUp/PgDn | Move 10 rows | - | - |
| Home/End | First/last row | - | - |
| Enter | Tab row: restore. Header: open/close | Keep the filter, back to moving | Cancel |
| Right, l | Open the group | - | - |
| Left, h | Header: close. Tab row: jump to its header and close | - | - |
| / | Start typing a filter | Typed as text | Cancel |
| d | Ask to delete the highlighted tab | Typed as text | Cancel |
| y | - | Typed as text | Delete |
| Backspace | - | Delete a character; on empty text, back to moving | Cancel |
| Esc | Clear the filter if one is set, else close | Clear the filter, back to moving | Cancel |
| q | Close | Typed as text | Cancel |

j, k, h, l, d, q and / are typed as filter text while filtering. Any key not in
the table cancels a delete confirmation.

## Filter

- A case-insensitive substring match against the tab label, workspace label
  and agent names.
- The status line shows `/` and the text being typed.
- While a filter is set, groups with a match are shown open and groups
  without one are hidden. Clearing the filter restores the groups' previous
  open state.
- After each keystroke the cursor moves to the first matching tab row. No
  match shows `No match` in the window.

## Mouse

- A click on a tab row highlights it; a click on a header opens or closes it.
- A double-click on a tab row restores it.
- The wheel moves the cursor three rows.
- Clicks outside the list window are ignored.
- Python 3.9 may not export `BUTTON5_PRESSED`. Look it up with `getattr` and
  treat a missing constant as "no wheel-down event".

## Actions and messages

- **Restore:** the status line shows `Restoring <label>...` before the restore
  runs.
  - Success closes the popup; warnings go to a notification, as today.
  - A busy sweep lock shows `A sweep is running; try again in a moment.`
  - Any other failure shows `Restore failed: <reason>`.
  - The popup stays open after a failure, and Ctrl-C stays ignored during a
    restore, as today.
- **Delete:**
  - `d` shows `Delete "<label>"? This cannot be undone. [y/N]`.
  - `y` shows `Waiting for a sweep to finish...` while it waits up to
    `DELETE_LOCK_WAIT_SECONDS` for the sweep lock, then deletes.
  - A busy lock shows the same sweep-running message.
- **Messages:** a message stays until the next key press.
- **After any action:** the archive is re-read, and the cursor stays on the
  same entry id if it still exists (otherwise on the row at the same
  position).
- **Empty archive:** the popup shows `No archived tabs.` and
  `Press any key to close.`

## Architecture

`shelf/picker.py` is rewritten. `days_idle`, `record_label` and `agent_names`
stay, since `restore.py` and `__main__.py` import them. `parse_choice`, the
`input_fn`/`print_fn` parameters and the `N more; run python3 -m shelf list`
line are removed.

Pure logic, testable without a terminal:

- `State`: a dataclass holding the records, cursor, scroll offset, the set of
  open groups, filter text, mode (`move`, `filter`, `confirm`) and status
  message.
- `initial_state(records, now)` applies the group rules above. `now` is a
  timezone-aware local datetime (the loop passes `now_fn().astimezone()`), so
  tests choose the time zone without setting `TZ`.
- `reduce(state, event)` updates `state` in place and returns an action or
  `None`.
  - Events are strings (`"up"`, `"down"`, `"pgup"`, `"pgdn"`, `"home"`,
    `"end"`, `"enter"`, `"left"`, `"right"`, `"backspace"`, `"esc"`,
    `"wheelup"`, `"wheeldown"`) or tuples (`("char", c)`, `("click", row)`,
    `("dclick", row)`, `("resize", h, w)`).
  - Actions: `("restore", id)`, `("delete", id)` or `("quit",)`.
- `refresh(state, records)` swaps in a re-read archive and keeps the cursor
  on the same id.
- `render(state)` returns the lines to draw and the index of the highlighted
  line. The popup size lives in `state` (set by the `resize` event).
- `layout(height)` and `window_row(state, y)` say which screen lines hold the
  list, so a mouse click maps to a row.
- `apply(state, action, arch, do_restore, notify, draw)` carries out an
  action: it deletes under the sweep lock or restores, sets the status
  message, and returns True when the popup should close. It never touches
  curses; `draw` is a callback.

`reduce` adjusts the scroll offset so that the cursor always stays inside the
10-row window.

Terminal loop: `run(arch, do_restore, now_fn, notify)` sets up curses, turns
input into events, and carries out actions.

- **Setup:**
  - `os.environ.setdefault("ESCDELAY", "25")` before curses starts, so a
    single Esc closes the popup within about 25 ms instead of ncurses' default
    1 s.
  - `curses.wrapper`, `keypad(True)`, `curs_set(0)`, and `mousemask` for
    clicks and the wheel.
- **Input:** `get_wch` reads keys, so the filter accepts any character.
- **Esc versus arrow keys:** in keypad mode herdr sends arrows as `ESC O B`,
  which curses decodes. If herdr ever sent `ESC [ B` instead, curses would
  return the three bytes separately, and the first would read as Esc and
  close the popup. So on a lone Esc, the loop reads the next byte without
  blocking. If it is `[` or `O` followed by `A`-`D`, the loop maps it to an
  arrow; otherwise it is Esc.
- **Resize:** `KEY_RESIZE` becomes `("resize", h, w)` from `getmaxyx()`.
- **Actions:** restore and delete run with the same locking as today. The
  loop draws the status message first, then does the work.

herdr (0.9.1) sends every key, including Esc, Ctrl-C and the prefix key, to a
popup, and closes it only when its command exits or on `popup.close`. So the
picker must close itself on every path, and nothing it binds conflicts with
herdr.

`__main__._pick` keeps its error handling. If curses fails to start, or the
picker raises, the terminal is restored first (by `curses.wrapper`), and then
the existing handler prints the error and waits for Enter. `_pick` no longer
passes `input_fn` or `print_fn`, and the test that checked those arguments is
removed.

While the picker runs, `_pick` removes the logger's stderr handler and puts
it back afterwards: a restore logs several lines, and on stderr they would
draw over the curses screen. `shelf.log` still gets every line.

Only features available in Python 3.9 are used; the test matrix stays
3.9 and 3.12.

## Testing

`tests/test_picker.py` is rewritten around the pure functions.

**Reducer tests:**
- Movement and bounds; PgUp/PgDn/Home/End.
- Opening and closing groups; Left from a tab row.
- Group boundaries: local midnight, 6 and 7 days, 29 and 30 days, a UTC
  timestamp that falls on a different local date, and a missing or
  unparseable `archived_at`.
- "Older" opening when it is the only group with entries.
- Sorting within a group, including the same-sweep tie-break.
- Filter: typing, no match, Enter, Backspace on empty text, Esc clearing it,
  and restoring the open groups afterwards.
- Esc with a filter set, then without.
- Delete confirm with `y`, `N`, Esc and another key.
- Enter, `q` and double-click producing the right action.
- The scroll offset keeping the cursor inside the window.
- Mapping a click row to an entry.
- `refresh` keeping the cursor on the same id, and falling back when the id
  is gone.

**Render tests:**
- Exact lines for an 11-entry fixture at 80x18.
- Narrow widths.
- Both scroll markers.
- Dropping rows as the height shrinks, and `Popup too small`.
- The details line on a tab row and on a header.
- The empty archive.

**Terminal test** (`tests/test_picker_tty.py`):
- `pty.fork` runs `run()` against a temporary archive and a `do_restore`
  that records the id it was given.
- The parent sends Down then Enter as `ESC O B` `\r`, and in a second case as
  `ESC [ B` `\r`. It then checks that the second entry was restored and that
  the child exited.
- The test is skipped if curses cannot start.

Verification before release:

- **Live check in herdr:**
  1. Archive a scratch tab with `python3 -m shelf archive <tab-id>`.
  2. Open the popup with the picker key.
  3. Move, open and close groups, filter, and scroll.
  4. Check that Esc closes the popup with one press.
  5. Delete one scratch entry and restore another.
- **Platforms:** on Linux and macOS, with the `python3` that herdr runs, check
  that `import curses` works and that one Esc closes the popup.

## Docs and release

- README "Restore": the new keys, groups, scrolling and filter; the Esc then
  Enter instruction is removed.
- AGENTS.md: the `shelf/picker.py` line (reducer, renderer, curses loop).
- The 2026-09-24 design spec's picker section. The spec's line saying the
  picker uses "plain `input()` with no curses dependency" is replaced.
- CHANGELOG 0.4.0: a behaviour-change note that the number shortcuts are gone
  and the popup height is now 18 rows.
- `herdr-plugin.toml`: the version, and the popup `height = 18`.
