# herdr-shelf design

- Date: 2026-09-24
- Status: implemented, v0.1.0
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
- Each sweep also records "active now" for any pane currently `working`, as a
  backstop for missed events. `blocked` is not refreshed this way; otherwise a
  tab left waiting for input would never age.
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

For both, the session id must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`; anything
else is rejected before it is ever used to build a glob pattern. When several
files match an id, the one with the newest modification time is used.

A history source that fails to read its file is ignored for that sweep; the
generic signal still applies.

### Effective activity

The plugin records its own install time once, the first time any sweep (dry-run,
live, or a manual `archive`) ever runs: an ISO timestamp written to `installed_at`
in the state directory if that file does not already exist. Every session already
open at that first sweep gets `first_seen` equal to this same instant.

For a session, effective activity is the latest of:

- the last "active now" recorded by generic tracking;
- the history source's timestamp, if the agent has one and it succeeded;
- `first_seen`, when the agent has no history source or it failed, exactly as
  before; when the agent does have a history source, `first_seen` counts as
  activity too, but only when it is strictly later than `installed_at` -- a
  session first seen sometime after install, whose history transcript happens
  to be old, for example a conversation resumed by hand. A session seen at the
  very first sweep has `first_seen == installed_at` (not strictly later), so
  for those the history timestamp alone decides;
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
| `claude` | `claude` | `--resume {id}` | `-r <x>`, `--continue`, `-c`, `--session-id <x>`, `--fork-session` |
| `codex` | `codex` | `resume {id}` | a `resume <x>` subcommand, `--last`, `--all` |
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

A few rules make stripping and relaunching safe against surprising saved argvs:

- A flag's value is only consumed when the next token does not start with `-`;
  otherwise only the flag itself is removed, so a following option is never
  swallowed as if it were that flag's value.
- `argv[0]` becomes the bare program name when its basename is the program (so
  the program is looked up on `PATH` at restore time instead of a versioned
  path that may have since been deleted); it is left alone otherwise, for
  example when the saved command is a wrapper like `node`.
- If the stripped argv still contains a token with whitespace or a `--` token,
  the plain relaunch is used instead, so a prompt is never sent twice. A
  one-word positional prompt such as `claude hello` cannot be detected this
  way (no whitespace, no `--`) and is sent again; `"relaunch": "plain"` is the
  fix for that case.
- Session values must be non-empty, contain no C0 or C1 control characters,
  and not start with `-`. Capped at 512 characters, except for agents whose
  session values can be filesystem paths (`pi`, `omp`), which allow up to
  4096.

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
| `shelf/__main__.py` | Command-line entry: `track`, `sweep [--if-due]`, `archive <tab-id>`, `open-picker`, `pick`, `list`, `restore <id>` | all of the above |

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
height = "80%"
command = ["python3", "-m", "shelf", "pick"]
```

Sweeps are driven by the startup hook and the `workspace.focused` event
because herdr plugins have no timer. A sweep only runs when herdr is in use,
which is when tab clutter matters. Only one focus event is subscribed to:
herdr's `emit_focus_api_events` (`src/app/api.rs`) fires `workspace.focused`,
`tab.focused` and `pane.focused` together for every focus change, so
subscribing to more than one of them would only spawn the sweep hook process
multiple times per focus change without adding any coverage.

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
activity.json                        {"<agent>:<session>": {"first_seen", "last_active", "restored_at", "last_status"}}
activity.lock                        held while activity.json is read and rewritten
archive/<archive-id>/record.json
archive/<archive-id>/sessions/...    copies of Claude session files and directories
installed_at                         ISO time of the first sweep ever run
last_sweep                           ISO time of the last completed sweep
sweep.lock                           held for the duration of a sweep, a restore, or a picker delete
config-error.lock                    held while a broken config.json is reported
config-error-notified                mtime marks the last "config.json is invalid" notification
shelf.log                            one line per decision or error
shelf.log.1                          shelf.log rotated out once it passes 1MB
```

`last_status` (per session, alongside `first_seen`/`last_active`/`restored_at`) is
the last `agent_status` recorded for that session, used to tell a real status
change (which counts, for `blocked` and `done`) from herdr re-firing the same
status because only a pane's title or labels changed.

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

`layout.focused_pane_id` and `layout.zoomed` are recorded as they were captured,
but are informational only: restore does not apply either of them (`layout.apply`
has no way to request a starting focused pane or a zoomed pane, so there is
nothing to apply them to). They are kept in the record for a human reading it,
not removed.

## Flows

### Track (`track`, on `pane.agent_status_changed`)

1. Read `pane_id` and `agent_status` from `HERDR_PLUGIN_EVENT_JSON`.
2. If the status is not `working`, `blocked` or `done`, exit.
3. `pane.get` for the pane's `agent_session`. If there is none, exit.
4. Record "active now" for `<agent>:<session value>`, subject to the 60-second
   skip. `working` always counts; `blocked` and `done` count only on a change
   of status from the last status recorded for that session, because herdr
   also fires this event when only a pane's title or labels change. This means
   that if a `working` event is lost (for example the plugin was not running),
   a following `done` with the same status as the one already on record is
   not counted either.

### Sweep (`sweep --if-due`)

1. Exit if `last_sweep` is newer than `sweep_interval_minutes`. `sweep` without
   `--if-due` skips this check. Take `sweep.lock`, exiting if another sweep
   already holds it, then repeat the interval check once more now that the
   lock is held, in case another sweep ran (and updated `last_sweep`) in the
   gap between the first check and acquiring the lock.
2. List tabs and panes. For each pane read `agent_session` and agent status;
   record `first_seen` for new sessions and "active now" for panes that are
   `working`; compute effective activity.
3. Select eligible tabs.
4. In `dry-run`, log each eligible tab and show one notification, for example
   `shelf (dry-run): would archive 3 tabs: fix-retries, docs-pass, perf-probe`.
5. In `live`, archive each eligible tab, then show one summary notification.
   herdr compacts ids when a tab closes, so before archiving each tab the sweep
   lists tabs again, finds the tab by its panes' `terminal_id`s, and re-checks
   eligibility on that fresh data. A single tab's failure (herdr error,
   filesystem error, or anything unexpected) is logged with its traceback and
   reported as failed; it never aborts the rest of the sweep.
6. Show the summary notification in a `finally`, so it happens even if
   something above raised partway through, and exactly once. `last_sweep` is
   written in that same `finally`, but only when the initial gather (tab.list
   and pane.list) succeeded: if herdr could not even be listed, the next
   check (for example a focus event) should retry rather than wait out the
   interval. Then release the lock.

### Archive one tab

1. `layout.export` for the tab, then check that the tab's own pane ids match
   the layout's pane ids exactly. herdr 0.9.0 tab/pane ids are positional, so
   a close elsewhere between gathering the tab's panes and this step can mean
   the tab id we hold now points at a different tab; a mismatch skips the tab
   rather than archiving the wrong one.
2. For each agent pane, `pane.process_info`, and among the foreground
   processes whose `name`, or the basename of whose `argv[0]`, equals the
   agent table's `program`, take the one whose remaining arguments (`argv[1:]`,
   as a set) are a subset of every other match's remaining arguments -- a
   wrapper's argv is the user's flags plus whatever it injected, so the
   user-typed, outermost process is the smallest. When several matches
   qualify, the first in herdr's own process order wins. When none qualifies
   (for example an unrelated `claude mcp serve` child also matches the
   program name, but is not a wrapper around the interactive process), or no
   process matches at all (some installs run the agent under a wrapper), the
   pane is archived without a launch argv and restores the way
   `"relaunch": "plain"` does.
3. Record the tab label (treating a purely numeric label, herdr's default,
   as no custom label), the workspace label, and the first pane's cwd as the
   workspace cwd (herdr's workspace info has no cwd).
4. For Claude panes with `keep_transcripts`, copy the session file and its
   companion directory.
5. Write the record. If copying a session file or writing the record fails
   partway through, the partially written archive folder is removed rather
   than left behind as a corrupt entry.
6. Verify the tab's panes are unchanged right before `tab.close`: `pane.list`
   again and compare the tab's current `terminal_id`s against the ones
   gathered in step 1. A mismatch (another tab closed in between and shifted
   the ids) deletes the record and skips the tab instead of closing the wrong
   one.
7. `tab.close`. On `confirmation_required`, delete the record and log a skip.
   On any other definite refusal, delete the record and re-raise. When the
   outcome is unknown (the reply was lost), `pane.list` once more: if a tab
   with exactly the original `terminal_id`s still exists, the close never
   took effect and the record is deleted; otherwise the close did happen and
   the record -- possibly the only surviving copy -- is kept. Either way the
   error is re-raised.

The record is written before the tab is closed, so a failed close loses nothing.

Archive ids are `YYYYMMDDTHHMMSSZ-<6 hex chars>`. Every lookup by id (load,
delete, and the folder used to copy session files in and out) validates the
id against that pattern first, so a corrupted or handcrafted id can never
resolve to a path outside the archive directory.

### Restore one archived tab

Runs under `sweep.lock` (waiting up to 30 seconds for it), so a restore never
races a sweep, or a second restore of the same entry, over the same archive
directory. The record is (re-)loaded once the lock is held; if it is gone by
then (for example another process already restored it), the lookup raises
rather than restoring a stale copy.

1. Find the workspace whose label matches the record, taking the first match in
   sidebar order. If none exists, create it with `workspace.create` using the
   saved label and cwd.
2. If a Claude session file is missing and the archive holds a copy, copy it back
   to its original path. A pane whose saved cwd no longer exists on disk adds a
   warning ("... no longer exists; the pane opens in herdr's fallback
   directory") rather than failing the restore.
3. Build the `layout.apply` tree from the saved layout. Each agent pane's
   `command` becomes
   `["sh", "-c", "trap : INT; <relaunch argv>; exec \"${SHELL:-sh}\""]`, with every
   argument shell-quoted. The relaunch argv is the saved launch argv with the agent's
   resume tokens stripped and its resume arguments appended, or `<program> <resume args>`
   when the agent's `relaunch` is `plain`. The `trap` ignores SIGINT in the wrapper
   shell itself, so a Ctrl-C aimed at the agent does not kill the pane before the
   fallback shell can start. The shell wrapper leaves a usable shell when the agent
   exits. Shell panes get no command and start a shell in their cwd.
4. Apply with the saved tab label and `focus: true`. `layout.apply` rejects a
   request that carries both `tab_id` and `workspace_id`, so when step 1 created
   a new workspace, only its first tab's `tab_id` is sent (never `workspace_id`
   too); when an existing workspace was found, only `workspace_id` is sent. If
   apply fails after a workspace was just created for this restore, the new
   workspace is closed (best effort; a failure to close it is logged, not
   raised) before the original error is re-raised, so a failed restore does not
   leave a stray empty workspace behind.
5. Set `restored_at` for each restored session, then delete the archive entry.
   A failure while recording `restored_at` is logged but never keeps the
   entry: once `layout.apply` has succeeded, the tab is live and the archive
   entry must not linger just because bookkeeping afterward had a problem.

If a Claude session file is gone and no copy exists, the agent starts with its
relaunch argv anyway, and a notification says the conversation may not resume.
For other agents, their own session retention applies.

### Picker (`pick`)

Opened by the `restore` action through `plugin.pane.open`. It lists archived tabs
newest first: number, tab label, workspace label, agent, and days since last
activity. Typing a number restores that entry, `d <number>` deletes one, and `q`
closes the popup. It uses plain `input()` with no curses dependency.

### Manual archive (`archive <tab-id>`)

Archives one tab now, ignoring `idle_days` and `mode`, but still refusing a tab
that is focused, working, or has an agent pane without a session id or table
entry. Used for the release check and for archiving a tab by hand.

## Failure handling

- Hooks always exit 0. If the herdr socket is unavailable they do nothing.
- An error on one tab is logged and the sweep continues with the next tab.
- Errors go to `shelf.log` and to stderr, which herdr keeps in `herdr plugin log`.
- A restore that fails to apply the layout keeps the archive entry and shows the
  error in the popup itself (not a notification): the picker prints "Restore
  failed: ..." and waits for Enter before re-rendering the list, so the entry is
  still there to retry. Once `layout.apply` has succeeded, though, the tab is
  live and the entry is always deleted, even if recording `restored_at`
  afterward fails; that failure is only logged.
- Locks use `fcntl.flock`, so a lock held by a process that dies is released by
  the kernel; lock files are never deleted.

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
   key = "prefix+shift+s"        # free in the default keymap; prefix+s is settings
   type = "plugin_action"
   command = "anilkmr.shelf.restore"
   description = "restore an archived tab"
   ```

2. Read the dry-run notifications for a few days. Tabs whose agent has a history
   source can show up in the first sweep. Other agents' tabs appear no earlier
   than `idle_days` after install.
3. Set `"mode": "live"`.
