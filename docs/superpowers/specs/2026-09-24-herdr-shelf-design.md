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
original launch command. Work with every agent herdr can resume, not only a
chosen few.

## Non-goals

- Tabs with no agent pane. They are never archived.
- Agents herdr detects but has no session id for (no official integration
  installed, or the integration does not report one). There is nothing to resume,
  so their tabs are never archived.
- Preserving live processes, PTYs or scrollback. A restore is a rebuild.
- Pin, snooze and a per-sweep cap. Every archive is restorable, so these are left
  out until real use shows a need.
- Windows.

## What herdr provides (0.9.0)

These facts were checked against herdr's source and a live 0.9.0 server:

- The API exposes no per-tab or per-pane "last used" timestamp, so the plugin must
  measure activity itself.
- The `pane.agent_status_changed` event carries `pane_id` and the new
  `agent_status` (`working`, `blocked`, `done`, `idle` or `unknown`) for any agent
  herdr detects. Plugins can hook it.
- `pane.get` returns `agent_session` (`agent`, `kind`, `source`, `value`) when an
  official herdr integration reported a native session id.
- The `tab.closed` event carries only `tab_id` and `workspace_id`, so a tab cannot
  be reconstructed after it closes. Everything must be captured before closing.
- `layout.export` returns a tab's split tree with each pane's `pane_id`, `label`,
  `cwd` and launch `command`. `layout.apply` builds a new tab from such a tree in a
  given workspace. Its pane nodes have no session field, and neither call
  preserves processes.
- herdr's own resume commands live in `src/agent_resume.rs` and are used only when
  the server restarts. They are not exposed through the API: `agent.start` takes a
  kind and caller-supplied args, nothing more. The plugin therefore carries its own
  copy of that table.
- `pane.process_info` returns the pane's foreground processes with their argv.
- `tab.close` on a workspace's only tab closes the workspace too. If the workspace
  belongs to a worktree group, it fails with `confirmation_required`.
- Plugins get actions, event hooks (with `HERDR_PLUGIN_EVENT_JSON`), a startup
  hook, popup panes, and per-plugin config and state directories. There is no
  timer.
- `herdr notification show` displays a notification. `plugin.pane.open` opens a
  plugin pane such as a popup.

## Activity signal

"Activity" means the agent did something, not that the user looked at the tab.
It is tracked per agent session, keyed as `<agent>:<session value>`, because herdr
reuses pane and tab ids.

### Generic tracking (all agents)

- The `pane.agent_status_changed` hook records "active now" for the pane's session
  when the new status is `working`, `blocked` or `done`. Those states mean the agent
  ran, asked for input, or finished. `idle` means a finished agent was seen by the
  user and `unknown` means herdr could not classify it; neither counts.
- Each sweep also records "active now" for any pane currently `working` or
  `blocked`, as a backstop for missed events.
- A session seen for the first time gets `first_seen = now`. With generic tracking
  alone, no tab is archived until `idle_days` after the plugin first sees it.

### History sources (optional, per agent)

A history source reads an agent's own session files to find its last activity,
including activity from before the plugin was installed. Two ship with the plugin:

- **Claude Code:** the timestamp of the last `user` or `assistant` entry, skipping
  entries with `isMeta` or `isSidechain`, in
  `$CLAUDE_CONFIG_DIR/projects/*/<session-id>.jsonl` (default `~/.claude`). File
  modification time is not usable: Claude Code appends bookkeeping entries without
  timestamps (for example `cost-state` and `permission-mode`) to idle sessions, so
  an idle session's file can look fresh while its last message is weeks old.
- **Codex:** the timestamp of the last `response_item` or `event_msg` entry in
  `$CODEX_HOME/sessions/**/rollout-*-<session-id>.jsonl` (default `~/.codex`).

A history source that fails to read its file is ignored for that sweep; the
generic signal still applies.

### Effective activity

For a session, effective activity is the latest of:

- the last "active now" recorded by generic tracking;
- the history source's timestamp, if the agent has one and it succeeded;
- `first_seen`, only when the agent has no history source or it failed;
- `restored_at`, the last time this plugin restored the session. Without it, a
  restored tab would be archived again on the next sweep.

## Eligibility

A tab is archived when all of the following hold:

1. It has at least one agent pane.
2. Every agent pane has an `agent_session`, its agent has an entry in the agent
   table, and its effective activity is older than `idle_days`.
3. No pane in the tab has agent status `working`.
4. The tab is not the focused tab.

Blocked agents are treated like any other: blocked with no activity for
`idle_days` is archived. Shell panes inside an eligible tab are archived with it
and come back as shells in their working directory.

Anything uncertain makes the tab ineligible for that sweep, for example a failed
`pane.process_info`.

## Agent table

One table, `shelf/agents.py`, drives resume for every agent. Each entry has the
program name to look for in the pane's foreground processes, the resume arguments
as a template, and the tokens to strip from a saved launch command before the
resume arguments are appended. It mirrors herdr's `src/agent_resume.rs`.

| herdr agent | Program | Resume args | Also stripped |
|---|---|---|---|
| `claude` | `claude` | `--resume {id}` | `-r <x>`, `--continue`, `-c` |
| `codex` | `codex` | `resume {id}` | a `resume <x>` subcommand |
| `copilot` | `copilot` | `--resume={id}` | |
| `devin` | `devin` | `--resume {id}` | |
| `droid` | `droid` | `--resume {id}` | |
| `kimi` | `kimi` | `--session {id}` | |
| `mastracode` | `mastracode` | `--thread {id}` | |
| `pi` | `pi` | `--session {id}` | |
| `omp` | `omp` | `--resume={id}` | `-r <x>` |
| `hermes` | `hermes` | `--resume {id}` | |
| `opencode` | `opencode` | `--session {id}` | |
| `qodercli` | `qodercli` | `--resume {id}` | |
| `qwen` | `qwen` | `--resume {id}` | |
| `kilo` | `kilo` | `--session {id}` | |
| `cursor` | `cursor-agent` | `--resume {id}` | |
| `agy` | `agy` | `--conversation {id}` | |
| `grok` | `grok` | `--resume {id}` | |
| `letta` | `letta` | `--conversation {id}` | see below |

Stripping always removes the template's own flag: a token equal to it together
with the following value, or a token starting with `<flag>=`. The "Also stripped"
column lists additional aliases.

`letta` is the one special case, matching herdr: a session value of
`default:<agent-id>` resumes with `--conversation default --agent <agent-id>`.

Users can add or override entries in config (see Configuration). An entry may set
`"relaunch": "plain"`, which ignores the saved launch command and runs
`<program> <resume args>` exactly as herdr does on a server restart. That is the
escape hatch for agents started with a positional prompt such as
`claude "fix the build"`, where keeping the saved argv would send that prompt again.

## Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `shelf/api.py` | Minimal client for herdr's local socket (one JSON request per line) | `HERDR_SOCKET_PATH` |
| `shelf/agents.py` | Agent table, config overrides, and building the relaunch argv | config |
| `shelf/activity.py` | Activity store: record "active now", `first_seen`, `restored_at`; compute effective activity | state directory, `history` |
| `shelf/history.py` | Optional history sources, a mapping from agent name to reader function (Claude, Codex) | agent session files |
| `shelf/sweep.py` | Due check and lock, eligibility, dry-run report or archive, one summary notification | `api`, `activity`, `archive` |
| `shelf/archive.py` | Create, list, load and delete archive records; copy Claude session files in and out | state directory |
| `shelf/restore.py` | Rebuild an archived tab and resume its agents | `api`, `agents`, `archive`, `activity` |
| `shelf/picker.py` | Popup UI listing archived tabs | `archive`, `restore` |
| `shelf/__main__.py` | Command-line entry: `track`, `sweep [--if-due]`, `open-picker`, `pick`, `list`, `restore <id>` | all of the above |

Supporting a new agent needs one row in the agent table (or a config override).
A history source for it is optional.

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
on = "pane.agent_status_changed"
command = ["python3", "-m", "shelf", "track"]

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

`config.json` in the directory printed by `herdr plugin config-dir anilkmr.shelf`.
JSON is used because Python 3.9 has no TOML parser and the agent overrides are
nested. All keys are optional.

```json
{
  "idle_days": 7,
  "mode": "dry-run",
  "sweep_interval_minutes": 60,
  "keep_transcripts": true,
  "agents": {
    "qwen": {"relaunch": "plain"},
    "myagent": {"program": "myagent", "resume": ["--load", "{id}"], "strip": ["-l"]}
  }
}
```

- `mode`: `dry-run` reports only; `live` archives.
- `keep_transcripts`: copy Claude session files into the archive, so a resume still
  works after Claude Code's `cleanupPeriodDays` (default 30) deletes the originals.
- `agents`: per-agent overrides merged over the built-in table. `strip` lists flags
  that take a value; flags without a value go in `strip_bare`.

## State directory layout

Under `HERDR_PLUGIN_STATE_DIR`:

```
activity.json                        {"<agent>:<session>": {"first_seen", "last_active", "restored_at"}}
archive/<archive-id>/record.json
archive/<archive-id>/sessions/...    copies of Claude session files and directories
last_sweep                           ISO time of the last completed sweep
sweep.lock                           held for the duration of a sweep
shelf.log                            one line per decision or error
```

`activity.json` is updated under a lock with write-to-temp-and-rename, because
`track` and `sweep` can run at the same time. `track` skips the write when the
session's `last_active` is less than 60 seconds old, which keeps a busy agent from
rewriting the file on every status flip.

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

### Track (`track`, on `pane.agent_status_changed`)

1. Read `pane_id` and `agent_status` from `HERDR_PLUGIN_EVENT_JSON`.
2. If the status is not `working`, `blocked` or `done`, exit.
3. `pane.get` for the pane's `agent_session`. If there is none, exit.
4. Record "active now" for `<agent>:<session value>`, subject to the 60-second
   skip.

### Sweep (`sweep --if-due`)

1. Exit if `sweep.lock` is held, or if `last_sweep` is newer than
   `sweep_interval_minutes`. `sweep` without `--if-due` skips the interval check.
2. List tabs and panes. For each pane read `agent_session` and agent status;
   record `first_seen` for new sessions and "active now" for panes that are
   `working` or `blocked`; compute effective activity.
3. Select eligible tabs.
4. In `dry-run`, log each eligible tab and show one notification, for example
   `shelf (dry-run): would archive 3 tabs: fix-retries, docs-pass, perf-probe`.
5. In `live`, archive each eligible tab, then show one summary notification.
6. Write `last_sweep`, release the lock.

### Archive one tab

1. `layout.export` for the tab.
2. For each agent pane, `pane.process_info`, and take the argv of the first
   foreground process whose program basename matches the agent table's `program`.
   If none matches, the tab is skipped.
3. Record the tab label and the workspace label and cwd.
4. For Claude panes with `keep_transcripts`, copy the session file and its
   companion directory.
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
   `["sh", "-c", "<relaunch argv>; exec \"${SHELL:-sh}\""]`, with every argument
   shell-quoted. The relaunch argv is the saved launch argv with the agent's resume
   tokens stripped and its resume arguments appended, or `<program> <resume args>`
   when the agent's `relaunch` is `plain`. The shell wrapper leaves a usable shell
   when the agent exits. Shell panes get no command and start a shell in their cwd.
4. Apply with the saved tab label and `focus: true`.
5. Set `restored_at` for each restored session and delete the archive entry.

If a Claude session file is gone and no copy exists, the agent starts with its
relaunch argv anyway, and a notification says the conversation may not resume.
For other agents, their own session retention applies.

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
- Lock files are created with `O_EXCL`. A lock older than 10 minutes is treated as
  stale and removed.

## Testing

Standard library `unittest`, run in CI on Ubuntu and macOS with Python 3.9 and
3.12.

- Agent table: for every entry, the relaunch argv built from a sample saved argv
  and session id, including stripping `--flag value`, `--flag=value` and the listed
  aliases, the `letta` special case, `relaunch: plain`, and config overrides.
- Quoting of arguments that contain spaces and quotes in the `sh -c` wrapper.
- Tracking: which statuses record activity, the 60-second skip, and `first_seen`.
- History sources against fixture files, including Claude files whose tail is only
  bookkeeping entries, and meta and sidechain entries that must be skipped.
- Effective activity: each combination of generic, history, `first_seen` and
  `restored_at`.
- Eligibility as a table of cases: working, blocked, focused, no session, agent not
  in the table, shell-only tab, recently restored, mixed agent and shell panes.
- Archive and restore against a fake herdr socket server that records requests.
  Tests check the `layout.apply` payload and the call order, including that the
  record is written before `tab.close`.
- Config parsing, including unknown keys and malformed JSON.

Before each release: compare the agent table with the current herdr
`src/agent_resume.rs` and add any new agents. On a real herdr server, run a
dry-run sweep and compare its list with the tabs expected; archive one Claude tab
and one Codex tab in live mode, restore both, and confirm each conversation
resumes with its original launch flags.

## Repository

- `herdr-plugin.toml` at the root, so `herdr plugin install <owner>/herdr-shelf`
  works.
- `shelf/`, `tests/`, `README.md`, `LICENSE` (MIT), `CHANGELOG.md`,
  `CONTRIBUTING.md`, `.github/workflows/ci.yml`.
- The GitHub topic `herdr-plugin`, which the herdr marketplace uses for discovery.
- Semver tags starting at `v0.1.0`.

## Rollout

1. Install. The default `"mode": "dry-run"` applies until changed. Add a keybinding for the picker to herdr's
   `config.toml`:

   ```toml
   [[keys.command]]
   key = "prefix+alt+s"          # free in the default keymap; prefix+s is settings
   type = "plugin_action"
   command = "anilkmr.shelf.restore"
   description = "restore an archived tab"
   ```

2. Read the dry-run notifications for a few days. Tabs whose agent has a history
   source can show up in the first sweep. Other agents' tabs appear no earlier
   than `idle_days` after install.
3. Set `"mode": "live"`.
