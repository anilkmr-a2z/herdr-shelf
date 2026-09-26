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
- The `pane.agent_detected` event fires when a pane's detected agent label
  changes (`previous_agent_label != agent_label`) or a pane's agent is
  released (`agent_released`); it carries `pane_id`, `workspace_id`, an
  optional `agent`, `released` (omitted when false) and an optional
  `final_status` (`src/api/schema/events.rs`, `emit_pane_state_update` in
  `src/app/api.rs`). herdr's own resume of a pane it restored on a server
  restart sets the agent label directly while restoring it, rather than
  through this detection path, so it never fires this event. Plugins can
  hook it. Its JSON envelope's own `"event"` field (and `"data"`'s `"type"`)
  use herdr's snake_case `EventKind` name, `"pane_agent_detected"` -- not the
  dot form used in the manifest and in `HERDR_PLUGIN_EVENT`.
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
- Plugins get actions, event hooks (with `HERDR_PLUGIN_EVENT`, the hook's own
  dot-form event name, and `HERDR_PLUGIN_EVENT_JSON`, the envelope), a startup
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
- The `pane.agent_detected` hook records that an agent (re)started in a pane:
  a truthy `agent` and `released` false means an agent is now running there,
  whether that is the user starting one for the first time, resuming an old
  conversation by hand (for example `claude --resume <id>`), or an agent
  restarting on its own. This is recorded per pane terminal (`pane.get`'s
  `terminal_id`), as `agent_started_at`, because starting an agent sends no
  user or assistant message of its own and updates no session's `last_active`
  or history timestamp -- without this, a freshly (re)started agent in an old
  session would still look exactly as idle as it did before it started.
  herdr fires this same event, with `agent` empty or `released` true, when an
  agent's pane is released (the process exited or the pane closed); those are
  ignored. herdr's own resume of a pane it restored itself (on a server
  restart) never fires this event at all -- the agent label is set directly
  while restoring the pane, not detected -- so there is no grace period to
  worry about here; every `pane.agent_detected` start is a genuine one.
- Because `terminal_id`s are not stable across a herdr restart, a sweep's own
  `_record_presence` copies each pane's terminal `agent_started_at` onto the
  session's own record too (keeping the later of the two), so the start
  survives even once its original terminal_id is gone. `track()` also writes
  it directly onto the session record at detection time, when `pane.get`
  already reports the pane's `agent_session`. See "Effective activity" and
  "State directory layout".

**Real event shapes.** herdr's own `EventKind` names (used for the JSON
envelope's own `"event"` field, and for `"data.type"`) are snake_case, for
example `"pane_agent_detected"` and `"pane_agent_status_changed"` -- not the
dot form (`"pane.agent_detected"`) used in the manifest's `[[events]] on =`
and in `HERDR_PLUGIN_EVENT` (the hook's own event name, passed to the hook
process as an environment variable). `track()` is handed both
`HERDR_PLUGIN_EVENT_JSON` (the envelope, snake_case) and `HERDR_PLUGIN_EVENT`
(env_event, dot form), and treats an event as `pane.agent_detected` when
either the env var equals `"pane.agent_detected"`, or the envelope's own
`"event"` is `"pane_agent_detected"` or `"pane.agent_detected"`, or
`data.type` is `"pane_agent_detected"` -- any one of the three is enough,
since which one is reliably present can vary. herdr also omits `"released"`
from the envelope entirely when it is false, and omits `"agent"` on a
release.

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
  restored tab would be archived again on the next sweep;
- `agent_started_at`, the session's own copy of the last time an agent was
  (re)started in some pane carrying this session (see "Generic tracking"
  above for how it gets there). Counted unconditionally, the same as
  `last_active` and `restored_at`.

On top of a session's own effective activity, a sweep's activity lookup
(`sweep._activity_lookup`) additionally takes the pane's own `terminal_id`
and returns the later of the session's effective activity and that
terminal's own `agent_started_at` (from the `"terminals"` map, before it has
necessarily been copied onto the session record by `_record_presence` -- see
State directory layout). This is belt-and-suspenders with the point above:
between track() recording a start and the next sweep's `_record_presence`
copying it onto the session, the terminal-keyed lookup alone already makes
it count.

## Eligibility

A tab is archived when all of the following hold:

1. It has at least one agent pane.
2. Every agent pane's `agent_session.agent` matches the pane's own current
   agent. herdr can leave a previous agent's session on a pane where a
   different agent now runs (for example the pane was reused); a session
   whose agent no longer matches is never used to decide or resume that pane.
3. Every agent pane has an `agent_session`, its agent has an entry in the agent
   table, and its effective activity is older than `idle_days`.
4. No agent pane's session value is also open in some other tab (see below).
5. No pane in the tab has agent status `working`.
6. The tab is not the focused tab.

Blocked agents are treated like any other: blocked with no activity for
`idle_days` is archived. Shell panes inside an eligible tab are archived with it
and come back as shells in their working directory.

**Duplicate conversations.** The same session id can be open in two tabs at
once (for example the same `claude --resume <id>` run twice, or a session
resumed by hand into a second tab while the first is still open). Archiving
either one, or restoring an archive back into a tab while the same
conversation is already open elsewhere, would leave the two tabs pointing at
one conversation, so both are left alone instead: a sweep builds a map from
session key to the set of `tab_id`s an agent pane carries that session in,
and a tab whose session is open in any other tab is skipped with
`"<pane>: conversation <id prefix> is also open in another tab"`. This map is
rebuilt from a fresh listing right before the per-target close, not reused
from the sweep's initial one, so a duplicate that appears only partway
through the sweep is still caught. Two panes of the *same* tab carrying the
same session (which a map keyed by `tab_id` alone cannot distinguish from a
single occurrence) are caught separately, inside `decide()` itself, and
skipped with `"... is also open in another pane"`. Restoring an archive
checks the same thing against currently live panes (see Restore flow) and
refuses rather than duplicating the conversation.

A manual `archive <tab-id>` (see "Manual archive" below) does not apply the
cross-tab duplicate rule -- it is an explicit, single-tab action, and
proceeds -- but logs a warning when the session is open elsewhere, since a
later restore of the resulting archive entry will refuse while that other
copy is still open.

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
| `shelf/session.py` | `herdr_session_name`: recover the herdr session name from `HERDR_SOCKET_PATH` | none |
| `shelf/migrate.py` | Self-healing, one-way merge of pre-0.3.0 root-level state into `sessions/default/` | state directory |
| `shelf/__main__.py` | Command-line entry: `track`, `sweep [--if-due]`, `archive <tab-id>`, `open-picker`, `pick`, `list`, `restore <id>`; gates every command on the session allowlist, computes each session's own state directory, and runs the migration | all of the above |

Supporting a new agent needs one row in the agent table (or a config override).
A history source for it is optional.

## Manifest

```toml
id = "shelf"
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
on = "pane.agent_detected"
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

The `pane.agent_detected` hook runs the same `track` command as
`pane.agent_status_changed`. Both hooks get `HERDR_PLUGIN_EVENT` set to
their own dot-form name (`"pane.agent_detected"` or
`"pane.agent_status_changed"`); `track` uses that, together with the event
JSON's own (snake_case) `"event"`/`"data.type"` fields, to tell them apart
-- see "Real event shapes" under Activity signal.

## Configuration

`config.json` in the directory printed by `herdr plugin config-dir shelf`.
JSON is used because Python 3.9 has no TOML parser and the agent overrides are
nested. All keys are optional.

```json
{
  "idle_days": 7,
  "mode": "dry-run",
  "sweep_interval_minutes": 60,
  "keep_transcripts": true,
  "sessions": ["default"],
  "agents": {
    "qwen": {"relaunch": "plain"},
    "myagent": {"program": "myagent", "resume": ["--load", "{id}"], "strip": ["-l"]}
  }
}
```

- `mode`: `dry-run` reports only; `live` archives.
- `keep_transcripts`: copy Claude session files into the archive, so a resume still
  works after Claude Code's `cleanupPeriodDays` (default 30) deletes the originals.
- `sessions`: the herdr sessions Shelf is allowed to act in (see "Herdr session
  allowlist" below). Defaults to `["default"]`; `"*"` allows every session.
- `agents`: per-agent overrides merged over the built-in table. `strip` lists flags
  that take a value; flags without a value go in `strip_bare`.

An unknown top-level key, or an unknown key inside an `agents` entry, is logged as
a warning and otherwise ignored; it does not fail config loading. A per-agent
`resume` must be a non-empty list of strings containing `"{id}"` in at least one
element (nothing to substitute the session id into, otherwise), and `program`, if
given, must be a non-empty string. `sessions` must be a non-empty list of
non-empty strings. Any of these failing raises `ConfigError`, same as an invalid
top-level value.

## Herdr session allowlist

herdr plugins are installed once per machine, and every herdr session's server
loads them. Each herdr session has its own socket -- the default session's is
`<herdr config dir>/herdr.sock`; a named session `<name>`'s is
`<herdr config dir>/sessions/<name>/herdr.sock` -- but a plugin's own state and
config directories are shared across every session on the machine, keyed only
by plugin id. Without an allowlist, Shelf would sweep every herdr session,
including one driven by another tool (for example an automation tool that runs
its own agents in a separate herdr session): archiving one of that session's
tabs would take it away from the tool managing it, out from under it.

`shelf/session.py`'s `herdr_session_name(socket_path)` recovers the session
name from `HERDR_SOCKET_PATH` alone. The path is normalized
(`os.path.normpath`) first, so `..` components and doubled separators resolve
the way a real filesystem path would, before any name is extracted from it.
It returns:

- the `<name>` in a normalized path ending `sessions/<name>/herdr.sock`, when
  `<name>` passes herdr's own session name rule (letters, digits, `.`, `_` or
  `-`, 1-64 characters, and not exactly `.` or `..`);
- `"default"` for anything that does not name a session at all, including a
  missing or empty socket path (used by `list`, the only command that runs
  without `HERDR_SOCKET_PATH` at all -- see Command line in the README);
- `None` -- a disabled session, never `"default"` -- when the path clearly
  names a session (it has a `sessions` component in the normalized path) but
  no valid name could be parsed there: a missing name, a name containing a
  path separator, or a name that fails the rule above. `None` is always
  treated as disabled by `__main__`, regardless of `sessions` in config.json
  (including `"*"`): with no reliable name, there is nothing to safely
  enable.

`config.session_enabled(cfg, name)` is `"*" in cfg["sessions"] or name in
cfg["sessions"]`. `config.sessions_for_gate(config_dir)` reads just the
`sessions` key for the gate check below, without raising or logging: if
`sessions` on its own is a valid non-empty list of non-empty strings, it is
used as-is even when some other key in config.json is invalid (a hook still
needs to know which session it may act in even when, say, `idle_days` is
broken); otherwise (a missing file, malformed JSON, a non-object top level, a
missing `sessions` key, or an invalid `sessions` value) it returns the
default `["default"]`.

`__main__` computes the session name once per invocation and gates every
command on it:

- The hooks (`track`, `sweep --if-due` -- which covers both the startup hook
  and the `workspace.focused` hook, since both run that same command -- and
  `open-picker`) return 0 immediately when the session is disabled, without
  writing any state. `open-picker`, and the manual `sweep` (the `sweep-now`
  action), additionally show a notification, "shelf is not enabled for herdr
  session `<name>`", instead of opening the popup or sweeping.
- The manual commands (`sweep`, `archive`, `list`, `restore`, `pick`) print
  `shelf is not enabled for herdr session '<name>'; add it to "sessions" in
  config.json` and exit 1 when the session is disabled; `pick` instead shows
  that same message in the popup and waits for Enter, like its other error
  paths, and exits 0 either way -- it is a popup pane's command, not a script
  whose exit code anything checks.
- The gate check itself (`config.sessions_for_gate`, above) never raises and
  never logs, so the "ignoring unknown key(s)" warning does not fire on every
  hook invocation, and an invalid config.json cannot make the gate check
  itself fail. A command that goes on to load config the normal way (with
  warnings) still raises `ConfigError` and, for a hook, still triggers the
  existing rate-limited "config.json is invalid" notification exactly as
  before this check existed.
- Migration (see State directory layout) runs before this gate, for every
  invocation regardless of session: it is a shared, root-level concern, not
  tied to any one session's allowlist, so it also runs -- and logs to the
  shared `shelf.log` -- from a disabled session's hooks.

## State directory layout

Under `HERDR_PLUGIN_STATE_DIR` (the plugin state root, shared by every herdr
session):

```
sessions/<name>/activity.json        {"<agent>:<session>": {"first_seen", "last_active", "restored_at",
                                                             "last_status", "agent_started_at"},
                                       "terminals": {"<terminal_id>": {"agent_started_at"}}}
sessions/<name>/activity.lock        held while activity.json is read and rewritten
sessions/<name>/archive/<archive-id>/record.json
sessions/<name>/archive/<archive-id>/sessions/...  copies of Claude session files and directories
sessions/<name>/installed_at         ISO time of the first sweep ever run, for this herdr session
sessions/<name>/last_sweep           ISO time of the last completed sweep, for this herdr session
sessions/<name>/sweep.lock           held for the duration of a sweep, a restore, or a picker delete
migrate.lock                         held while pre-0.3.0 root-level state is moved into sessions/default/
config-error.lock                    held while a broken config.json is reported
config-error-notified                mtime marks the last "config.json is invalid" notification
shelf.log                            one line per decision or error, tagged with the herdr session name
shelf.log.1                          shelf.log rotated out once it passes 1MB
```

Everything under `sessions/<name>/` is private to one herdr session -- `<name>`
being whatever `shelf/session.py`'s `herdr_session_name` recovers from that
invocation's `HERDR_SOCKET_PATH` (see Herdr session allowlist above). `archive`,
`activity`, `sweep` and `restore` all take that per-session directory as their
"state directory" argument; `restore` and the picker's delete lock derive
`sweep.lock`'s location from `Archive.root.parent`, so as long as every caller
constructs `Archive` (and `ActivityStore`) with the per-session directory, all
of a session's own files -- including the two lock files derived this way --
land together automatically. `shelf.log`, `shelf.log.1`, `config-error-notified`
and `config-error.lock` stay at the state root, shared across sessions, since
they are not about any one session's tabs.

**Migration.** Before this layout existed, `activity.json`, `archive/`,
`last_sweep` and `installed_at` lived directly at the state root, shared by
every herdr session that ran Shelf -- so a pre-0.3.0 archive can have come
from any herdr session, and all of them land in `sessions/default/`
regardless. `shelf/migrate.py`'s `merge_into_default_session(root)` runs on
*every* invocation, called from `main()` before the session gate and before
dispatch (so it runs for a disabled session too, logging to the shared
`shelf.log`) -- never once ever. The presence of `sessions/` is never treated
as "already migrated": a downgrade to a pre-0.3.0 build can write new
root-level state after an earlier merge (a rollback), and a merge interrupted
partway through must finish on a later run. Instead:

1. `root_legacy_present(root)` is a cheap, lock-free check: do any of
   `activity.json`, a non-empty `archive/`, `last_sweep` or `installed_at`
   still exist directly at the root? If not, `merge_into_default_session`
   returns immediately -- no lock taken, nothing created. This is what makes
   a fresh install, or an invocation from a herdr session with nothing of its
   own to migrate, free of side effects.
2. Otherwise it takes `migrate.lock`, plus the root-level `sweep.lock` and
   `activity.lock` -- the ones a still-running pre-0.3.0 process uses -- each
   waiting up to 10 seconds, so a pre-0.3.0 process mid-sweep or mid-`track`
   (for example right after a rollback) is excluded for the whole merge. A
   lock that cannot be acquired in time is logged as a warning and left for
   the next invocation to retry; it never raises.
3. Each of the four items is merged independently, never overwriting a
   destination that already exists:
   - `archive/<id>`: moved into `sessions/default/archive/<id>` unless that
     id already exists there, in which case the root copy is left in place
     with a logged warning (archive ids are validated on save, so they
     cannot be renamed to something like `<id>.migrated-<ts>` without
     breaking that validation; leaving it is the safe choice).
   - `activity.json`: moved as-is if the destination doesn't exist yet;
     otherwise merged session by session -- for each session key, whichever
     side has the later `last_active`, `restored_at` and `agent_started_at`
     wins, field by field, with every other field defaulting to the
     destination's; `"terminals"` is a union, keyed by terminal id, with the
     same later-wins rule per entry.
   - `last_sweep`: the destination wins whenever it already exists.
   - `installed_at`: the earlier of the two wins whenever both exist -- the
     plugin's install time should not appear to move later just because a
     session was migrated.
4. Whatever was actually moved is logged as one info line
   (`"migrated legacy state into sessions/default: <items>"`). If anything
   is still left at the root once the attempt is done (an archive id
   collision, or a failure logged along the way), that is logged as an
   error, but `merge_into_default_session` still returns normally: the
   command that triggered it always gets to run, and the next invocation
   retries whatever is left.

Concurrent callers (two commands firing at once, for example a hook and a
manual command) are serialized by the three locks above: the second one to
acquire them finds the first one's work already done (or already failed and
logged), and its own merge functions are no-ops for anything no longer at
the root, so nothing is duplicated or lost either way.

`last_status` (per session, alongside `first_seen`/`last_active`/`restored_at`) is
the last `agent_status` recorded for that session, used to tell a real status
change (which counts, for `blocked` and `done`) from herdr re-firing the same
status because only a pane's title or labels changed.

A session record's own `agent_started_at` is the last time an agent was
(re)started in some pane carrying that session; `track` writes it directly
when `pane.get` already reports the pane's `agent_session`, and a sweep's
`_record_presence` also copies it over from the pane's terminal entry (see
below), keeping the later of the two either way.

`"terminals"` is a reserved top-level key in `activity.json`, holding per-pane
data keyed by `terminal_id` rather than by session -- currently just
`agent_started_at`, set by `track` on a `pane.agent_detected` event (see
Activity signal). Every other top-level key is a `<agent>:<session>` session
record; any code that iterates `activity.json` (the sweep's own eligibility
pass, archiving, restoring) must skip this one rather than treat it as a
session. A sweep prunes `"terminals"` down to the terminal ids it actually
saw in that sweep's `pane.list`, except for an entry whose `agent_started_at`
is at or after the sweep's own "now" -- possibly a concurrent `track()` call
recording a pane that appeared after this sweep's own listing was taken,
which must not be pruned just because it postdates that listing. A
`"terminals"` value that is not a dict (or an individual entry that is not
a dict) is treated as empty/absent rather than raising. This pruning is also
how a terminal id survives a herdr restart in effect: `_record_presence`
copies each present pane's terminal `agent_started_at` onto its session's own
record *before* pruning runs, so by the time a restart's new terminal id
shows up with no history of its own, the session record it belongs to
already remembers the start under the old (now-gone) terminal id.

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
  "session_copies": ["projects/-home-user-src-api-service/<session-id>.jsonl"],
  "herdr_session": "default"
}
```

`herdr_session` is informational only: the herdr session (see Herdr session
allowlist above) the tab was archived from, for a human reading the record
later. It is not consulted for anything -- restoring an archive does not
check or require the current session to match it.

`layout.root` is the tree returned by `layout.export`, stored as is. Records are
written to a temporary file and renamed into place.

`layout.focused_pane_id` and `layout.zoomed` are recorded as they were captured,
but are informational only: restore does not apply either of them (`layout.apply`
has no way to request a starting focused pane or a zoomed pane, so there is
nothing to apply them to). They are kept in the record for a human reading it,
not removed.

## Flows

Migration (see State directory layout) runs first, ahead of every flow below
and regardless of the session gate. Every flow below is then gated on the
herdr session allowlist (see Herdr session allowlist above): `__main__`
computes the herdr session name from `HERDR_SOCKET_PATH` and checks it
against `sessions` before doing any of the work described in that flow. The
one exception is `sweep --if-due`'s own due check (step 1 below), which runs
before the gate rather than after it, so a hook invocation that is not due
skips both without ever loading config. A disabled session short-circuits
its flow at that gate -- a hook returns 0 without going any further
(`open-picker` and the manual `sweep` also show a notification first), and a
manual command prints (or, for `pick`, shows in the popup) that the session
is not enabled -- so nothing past that point runs and no state is written.
Exit codes on a disabled session follow each command's own convention:
1 for `sweep`, `archive`, `list` and `restore`; 0 for a hook; and 0 for
`pick`, which is a popup pane's command, not a script whose exit code
anything checks.

### Track (`track`, on `pane.agent_status_changed` or `pane.agent_detected`)

`track` is handed both `HERDR_PLUGIN_EVENT` (env_event; the hook's own
dot-form event name) and `HERDR_PLUGIN_EVENT_JSON` (the envelope, whose own
`"event"` and `"data.type"` are herdr's snake_case `EventKind` names
instead); an event is treated as `pane.agent_detected` when any of
`env_event == "pane.agent_detected"`, the envelope's `"event"` is
`"pane_agent_detected"` (or `"pane.agent_detected"`), or `data.type` is
`"pane_agent_detected"` -- see "Real event shapes" under Activity signal for
why all three are checked. Both hooks run the same `python3 -m shelf track`
command.

For `pane.agent_status_changed`:

1. Read `pane_id` and `agent_status` from the event data.
2. If the status is not `working`, `blocked` or `done`, exit.
3. `pane.get` for the pane's `agent_session`. If there is none, exit.
4. Record "active now" for `<agent>:<session value>`, subject to the 60-second
   skip. `working` always counts; `blocked` and `done` count only on a change
   of status from the last status recorded for that session, because herdr
   also fires this event when only a pane's title or labels change. This means
   that if a `working` event is lost (for example the plugin was not running),
   a following `done` with the same status as the one already on record is
   not counted either.

For `pane.agent_detected`:

1. Read `agent` and `released` from the event data. If `agent` is empty or
   `released` is true, exit -- this event also fires when a pane's agent is
   released, and that is not a start. herdr omits `"released"` entirely when
   it is false, and omits `"agent"` on a release.
2. `pane.get` for the pane's `terminal_id`. If there is none, exit.
3. Record `terminals.<terminal_id>.agent_started_at = now` (see State
   directory layout). If `pane.get`'s reply also carries the pane's
   `agent_session` (agent and value both present), also record
   `agent_started_at = now` directly on that session's own record, keeping
   whichever of the old and new values is later.

### Sweep (`sweep --if-due`)

1. Exit if `last_sweep` is newer than `sweep_interval_minutes`. `sweep` without
   `--if-due` skips this check. Take `sweep.lock`, exiting if another sweep
   already holds it, then repeat the interval check once more now that the
   lock is held, in case another sweep ran (and updated `last_sweep`) in the
   gap between the first check and acquiring the lock.
2. List tabs and panes. For each pane read `agent_session` and agent status;
   record `first_seen` for new sessions and "active now" for panes that are
   `working`; prune `terminals` entries for terminal ids not seen in this
   list; compute effective activity, including each pane's own
   `agent_started_at`. Also build the session-key -> set-of-`tab_id`s map used
   for the duplicate-conversation check.
3. Select eligible tabs (see Eligibility, including the duplicate-conversation
   check).
4. In `dry-run`, log each eligible tab and show one notification, for example
   `shelf (dry-run): would archive 3 tabs: fix-retries, docs-pass, perf-probe`.
5. In `live`, archive each eligible tab, then show one summary notification.
   herdr compacts ids when a tab closes, so before archiving each tab the sweep
   lists tabs again, finds the tab by its panes' `terminal_id`s, rebuilds the
   duplicate-conversation map from that fresh list, and re-checks eligibility
   on that fresh data. A single tab's failure (herdr error, filesystem error,
   or anything unexpected) is logged with its traceback and reported as
   failed; it never aborts the rest of the sweep.
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
   rather than archiving the wrong one. The layout is then checked against
   herdr's own `layout.apply` limits (24 panes, 16 levels deep); a layout past
   either limit is skipped rather than archived, since it could never be
   restored.
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
3. Before building anything or calling `workspace.create`: `pane.list` for every
   currently live pane that is running an agent (`agent` truthy -- matching
   the same rule sweep's own eligibility check uses; a pane with a leftover
   `agent_session` but no `agent` running does not count). If any such live
   pane's `agent_session.value` equals one of this record's own session
   values, the conversation is already open in another tab; raise
   `Skip("conversation <id prefix> is already open in another tab; close it
   first")` and leave the archive entry untouched. This
   is the same duplicate-conversation rule sweep applies (see Eligibility),
   checked again here because the tab could have been reopened by hand (or by
   a second restore of a session shared between two archive entries) since the
   archive entry was written.
4. Build the `layout.apply` tree from the saved layout. Each agent pane's
   `command` becomes
   `["sh", "-c", "trap : INT; <relaunch argv>; exec \"${SHELL:-sh}\""]`, with every
   argument shell-quoted. The relaunch argv is the saved launch argv with the agent's
   resume tokens stripped and its resume arguments appended, or `<program> <resume args>`
   when the agent's `relaunch` is `plain`. The `trap` ignores SIGINT in the wrapper
   shell itself, so a Ctrl-C aimed at the agent does not kill the pane before the
   fallback shell can start. The shell wrapper leaves a usable shell when the agent
   exits. Shell panes get no command and start a shell in their cwd.
5. Apply with the saved tab label and `focus: true`. `layout.apply` rejects a
   request that carries both `tab_id` and `workspace_id`, so when step 1 created
   a new workspace, only its first tab's `tab_id` is sent (never `workspace_id`
   too); when an existing workspace was found, only `workspace_id` is sent. If
   apply fails after a workspace was just created for this restore, the new
   workspace is closed (best effort; a failure to close it is logged, not
   raised) before the original error is re-raised, so a failed restore does not
   leave a stray empty workspace behind.
6. Set `restored_at` for each restored session, then delete the archive entry.
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
- `pane.agent_detected` tracking, using herdr's own (snake_case) event
  shapes: a start records `agent_started_at` for the pane's terminal, and
  also directly on the pane's own session record when `pane.get` already
  reports one; a released event or a missing agent is ignored; recognizing
  the event via `env_event` alone, or via `data.type` alone, with no reliance
  on the other.
- History sources against fixture files, including Claude files whose tail is only
  bookkeeping entries, and meta and sidechain entries that must be skipped.
- Effective activity: each combination of generic, history, `first_seen`,
  `restored_at` and `agent_started_at`.
- Eligibility as a table of cases: working, blocked, focused, no session, agent not
  in the table, shell-only tab, recently restored, mixed agent and shell panes,
  a duplicate conversation open in another tab, a duplicate conversation in
  two panes of the same tab.
- A sweep where a session's own history/first_seen is well past `idle_days` but
  its pane's terminal has a recent `agent_started_at`: not eligible. That same
  start surviving a simulated herdr restart (the pane gets a new terminal id).
  A sweep with two tabs sharing one session: neither archived, including when
  the duplicate only appears during the sweep's own per-target recheck.
  Terminal entries for vanished terminals are pruned, except one started at or
  after the sweep's own "now" (a race with a concurrent track() call); a
  non-dict `"terminals"` value or entry is tolerated rather than raising. A
  manual `archive <tab-id>` on a tab whose session is open elsewhere proceeds
  but logs a warning.
- Archive and restore against a fake herdr socket server that records requests.
  Tests check the `layout.apply` payload and the call order, including that the
  record is written before `tab.close`, that `archive.capture`'s recorded
  `last_activity` reflects a pane's terminal start, and that a restore whose
  conversation is already open in a live pane (running an agent; a stale
  `agent_session` on a pane with no agent running does not count) raises and
  keeps the archive entry.
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
   command = "shelf.restore"
   description = "restore an archived tab"
   ```

2. Read the dry-run notifications for a few days. Tabs whose agent has a history
   source can show up in the first sweep. Other agents' tabs appear no earlier
   than `idle_days` after install.
3. Set `"mode": "live"`.
