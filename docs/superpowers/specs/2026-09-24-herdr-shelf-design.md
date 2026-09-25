# herdr-shelf design

- Date: 2026-09-24
- Status: draft, pending review
- Target: herdr 0.9.0 or newer, Linux and macOS, Python 3.9 or newer, no third-party dependencies

## Problem

Herdr tabs pile up. A coding agent finishes a task, the tab stays open, and after a
few weeks there are dozens of tabs whose agents have not done anything in days.
Closing them by hand is risky because herdr has no undo: the layout, the working
directories and the agent's launch flags are gone once the tab closes.

## Goal

Archive agent tabs whose agents have been inactive for a configurable number of
days (default 7), and restore any archived tab on demand with its layout, working
directories, labels, and the agent conversation resumed using the agent's
original launch command.

## Non-goals

- Tabs with no agent pane. They are never archived.
- Preserving live processes, PTYs or scrollback. A restore is a rebuild.
- Agents other than Claude Code and Codex. Panes running other agents make their
  tab ineligible until a reader for that agent exists.
- Pin, snooze and a per-sweep cap. Every archive is restorable, so these are left
  out until real use shows a need.
- Windows.

## What herdr provides (0.9.0)

These facts were checked against herdr's source and a live 0.9.0 server:

- The API exposes no per-tab or per-pane "last used" timestamp, so the plugin must
  measure activity itself.
- The `tab.closed` event carries only `tab_id` and `workspace_id`, so a tab cannot
  be reconstructed after it closes. Everything must be captured before closing.
- `layout.export` returns a tab's split tree with each pane's `pane_id`, `label`,
  `cwd` and launch `command`. `layout.apply` builds a new tab from such a tree in a
  given workspace. Neither preserves processes.
- `pane.get` returns `agent_session` (`agent`, `kind`, `source`, `value`) when the
  agent's official herdr integration reported a native session id.
- `pane.process_info` returns the pane's foreground processes with their argv.
- `tab.close` on a workspace's only tab closes the workspace too. If the workspace
  belongs to a worktree group, it fails with `confirmation_required`.
- Plugins get actions, event hooks, a startup hook, popup panes, and per-plugin
  config and state directories. There is no timer.
- `herdr notification show` displays a notification. `plugin.pane.open` opens a
  plugin pane such as a popup.

## Activity signal

"Activity" means the agent did something, not that the user looked at the tab.

- **Claude Code:** the timestamp of the last `user` or `assistant` entry, skipping
  entries with `isMeta` or `isSidechain`, in
  `$CLAUDE_CONFIG_DIR/projects/*/<session-id>.jsonl` (default `~/.claude`).
- **Codex:** the timestamp of the last `response_item` or `event_msg` entry in
  `$CODEX_HOME/sessions/**/rollout-*-<session-id>.jsonl` (default `~/.codex`).
- **Effective activity** for a pane is the later of that timestamp and the time the
  session was last restored by this plugin. Without the second term, a restored tab
  would be archived again on the next sweep.

File modification time is not usable. Claude Code appends bookkeeping entries
without timestamps (for example `cost-state` and `permission-mode`) to idle
sessions, so an idle session's file can look fresh while its last message is
weeks old.

Claude Code deletes session files that have not been written for
`cleanupPeriodDays` (default 30). An archived session is never written, so the
archive keeps a copy of the session file and its companion directory.

## Eligibility

A tab is archived when all of the following hold:

1. It has at least one agent pane.
2. Every agent pane has an `agent_session`, an agent with a known reader, and an
   effective activity older than `idle_days`.
3. No pane in the tab has agent status `working`.
4. The tab is not the focused tab.

Blocked agents are treated like any other: blocked with no activity for
`idle_days` is archived. Shell panes inside an eligible tab are archived with it
and come back as shells in their working directory.

Anything uncertain makes the tab ineligible for that sweep: an unreadable or
missing session file, an agent without a reader, a failed `pane.process_info`.

## Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `shelf/api.py` | Minimal client for herdr's local socket (one JSON request per line) | `HERDR_SOCKET_PATH` |
| `shelf/activity.py` | Last activity time for a pane: a table mapping agent name to a reader function, plus the restored-at override | session files |
| `shelf/sweep.py` | Due check and lock, eligibility, dry-run report or archive, one summary notification | `api`, `activity`, `archive` |
| `shelf/archive.py` | Create, list, load and delete archive records, and copy session files in and out | state directory |
| `shelf/restore.py` | Rebuild an archived tab and resume its agents | `api`, `archive` |
| `shelf/picker.py` | Popup UI listing archived tabs | `archive`, `restore` |
| `shelf/__main__.py` | Command-line entry: `sweep [--if-due]`, `open-picker`, `pick`, `list`, `restore <id>` | all of the above |

Adding an agent means one reader function in `activity.py`, one row in the resume
table in `restore.py`, and tests for both.

## Manifest

```toml
id = "anilkmr.shelf"
name = "Shelf"
version = "0.1.0"
min_herdr_version = "0.9.0"
description = "Archive agent tabs that have been inactive for days, and restore them with their conversation resumed."
platforms = ["linux", "macos"]

[[startup]]
command = ["python3", "-m", "shelf", "sweep", "--if-due"]

[[events]]
on = "pane.focused"
command = ["python3", "-m", "shelf", "sweep", "--if-due"]

[[events]]
on = "workspace.focused"
command = ["python3", "-m", "shelf", "sweep", "--if-due"]

[[actions]]
id = "restore"
title = "Shelf: restore an archived tab"
contexts = ["global"]
command = ["python3", "-m", "shelf", "open-picker"]

[[actions]]
id = "sweep-now"
title = "Shelf: sweep now"
contexts = ["global"]
command = ["python3", "-m", "shelf", "sweep"]

[[panes]]
id = "picker"
title = "Shelf"
placement = "popup"
width = "80%"
height = 20
command = ["python3", "-m", "shelf", "pick"]
```

Sweeps are driven by the startup hook and focus events because herdr plugins have
no timer. A sweep only runs when herdr is in use, which is when tab clutter
matters.

## Configuration

`config.toml` in the directory printed by `herdr plugin config-dir anilkmr.shelf`. All keys
are optional.

```toml
idle_days = 7              # archive after this many days without agent activity
mode = "dry-run"           # "dry-run" reports only; "live" archives
sweep_interval_minutes = 60
keep_transcripts = true    # copy Claude session files into the archive
```

Python 3.9 has no `tomllib`, so the plugin parses this flat `key = value` subset
itself (strings, integers, booleans, comments).

## State directory layout

Under `HERDR_PLUGIN_STATE_DIR`:

```
archive/<archive-id>/record.json
archive/<archive-id>/sessions/...    copies of Claude session files and directories
restored.json                        {"<session-id>": "<ISO time>"}
last_sweep                           ISO time of the last completed sweep
sweep.lock                           held for the duration of a sweep
shelf.log                            one line per decision or error
```

`restored.json` is keyed by agent session id because herdr reuses tab ids.

## Archive record

```json
{
  "version": 1,
  "id": "20260924T101500Z-a1b2c3",
  "archived_at": "2026-09-24T10:15:00Z",
  "workspace": {"label": "api-service", "cwd": "/home/user/src/api-service"},
  "tab": {"label": "fix-retries"},
  "layout": {"root": {}, "focused_pane_id": "w1:p3", "zoomed": false},
  "panes": {
    "w1:p3": {
      "cwd": "/home/user/src/api-service",
      "agent": "claude",
      "session": {"kind": "id", "value": "<session-id>", "source": "herdr:claude"},
      "launch_argv": ["claude", "--model", "opus"],
      "last_activity": "2026-09-15T08:02:11Z"
    }
  },
  "session_copies": ["projects/-home-user-src-api-service/<session-id>.jsonl"]
}
```

`layout.root` is the tree returned by `layout.export`, stored as is. Records are
written to a temporary file and renamed into place.

## Flows

### Sweep (`sweep --if-due`)

1. Exit if `sweep.lock` is held, or if `last_sweep` is newer than
   `sweep_interval_minutes`. `sweep` without `--if-due` skips the interval check.
2. List tabs and panes. For each pane read `agent_session` and agent status, and
   compute effective activity.
3. Select eligible tabs.
4. In `dry-run`, log each eligible tab and show one notification, for example
   `shelf (dry-run): would archive 3 tabs: fix-retries, docs-pass, perf-probe`.
5. In `live`, archive each eligible tab, then show one summary notification.
6. Write `last_sweep`, release the lock.

### Archive one tab

1. `layout.export` for the tab.
2. For each agent pane, `pane.process_info`, and take the argv of the first
   foreground process whose program name matches the agent (`claude`, `codex`).
3. Record the tab label and the workspace label and cwd.
4. If `keep_transcripts`, copy the Claude session file and its companion directory.
5. Write the record.
6. `tab.close`. On `confirmation_required`, delete the record and log a skip.

The record is written before the tab is closed, so a failed close loses nothing.

### Restore one archived tab

1. Find the workspace whose label matches the record, taking the first match in
   sidebar order. If none exists, create it with `workspace.create` using the
   saved label and cwd.
2. If a Claude session file is missing and the archive holds a copy, copy it back
   to its original path.
3. Build the `layout.apply` tree from the saved layout. Each agent pane's
   `command` becomes
   `["sh", "-c", "<launch argv> <resume args>; exec \"${SHELL:-sh}\""]`,
   with the argv shell-quoted. The shell wrapper leaves a usable shell when the
   agent exits. Shell panes get no command and start a shell in their cwd.
4. Apply with the saved tab label and `focus: true`.
5. Write the session ids to `restored.json` and delete the archive entry.

Resume arguments:

| Agent | Removed from the saved argv first | Appended |
|---|---|---|
| `claude` | `--resume <x>`, `-r <x>`, `--continue`, `-c` | `--resume <id>` |
| `codex` | a trailing `resume <x>` subcommand | `resume <id>` |

If a session file is gone and no copy exists, the agent starts with its saved argv
and no resume arguments, and a notification says the conversation could not be
restored.

### Picker (`pick`)

Opened by the `restore` action through `plugin.pane.open`. It lists archived tabs
newest first: number, tab label, workspace label, agent, and days since last
activity. Typing a number restores that entry, `d <number>` deletes one, and `q`
closes the popup. It uses plain `input()` with no curses dependency.

## Failure handling

- Hooks always exit 0. If the herdr socket is unavailable they do nothing.
- An error on one tab is logged and the sweep continues with the next tab.
- Errors go to `shelf.log` and to stderr, which herdr keeps in `herdr plugin log`.
- A failed restore keeps the archive entry and shows a notification with the error.
- The sweep lock is a file created with `O_EXCL`. A lock older than 10 minutes is
  treated as stale and removed.

## Testing

Standard library `unittest`, run in CI on Ubuntu and macOS with Python 3.9 and
3.12.

- Activity readers against fixture files, including Claude files whose tail is
  only bookkeeping entries, and meta and sidechain entries that must be skipped.
- Eligibility as a table of cases: working, blocked, focused, no session, unknown
  agent, shell-only tab, recently restored, mixed agent and shell panes.
- Argv rewriting: removing old resume flags, appending resume arguments, quoting
  arguments that contain spaces and quotes.
- Archive and restore against a fake herdr socket server that records requests.
  Tests check the `layout.apply` payload and the call order, including that the
  record is written before `tab.close`.
- Config parsing, including comments and unknown keys.

Manual check before each release on a real herdr server: run a dry-run sweep and
compare its list with the tabs expected; archive one Claude tab and one Codex tab
in live mode, restore both, and confirm each conversation resumes with its
original launch flags.

## Repository

- `herdr-plugin.toml` at the root, so `herdr plugin install <owner>/herdr-shelf`
  works.
- `shelf/`, `tests/`, `README.md`, `LICENSE` (MIT), `CHANGELOG.md`,
  `CONTRIBUTING.md`, `.github/workflows/ci.yml`.
- The GitHub topic `herdr-plugin`, which the herdr marketplace uses for discovery.
- Semver tags starting at `v0.1.0`.

## Rollout

1. Install with `mode = "dry-run"` and add a keybinding for the picker to herdr's
   `config.toml`:

   ```toml
   [[keys.command]]
   key = "prefix+alt+s"          # free in the default keymap; prefix+s is settings
   type = "plugin_action"
   command = "anilkmr.shelf.restore"
   description = "restore an archived tab"
   ```

2. Read the dry-run notifications for a few days.
3. Set `mode = "live"`.
