# herdr-shelf

Archive [herdr](https://herdr.dev) tabs whose coding agents have been inactive for
days, and bring any of them back later with the conversation resumed.

Agent tabs pile up. herdr has no undo for a closed tab, so closing them by hand
means losing the layout, the working directories and the agent's launch flags.
Shelf closes inactive agent tabs for you and keeps everything needed to rebuild
them: the split layout, labels, working directories, each agent's session id,
and the command line the agent was started with.

## Install

```sh
herdr plugin install anilkmr-a2z/herdr-shelf
```

Requires herdr 0.9.0 or newer and `python3` 3.9 or newer on `PATH`. Linux and
macOS. On macOS, run `python3 --version` once first: the system `python3`
needs the Xcode Command Line Tools, and herdr plugin install does not check
that for you.

Add a key for the restore picker to `~/.config/herdr/config.toml`, then run
`herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+shift+s"
type = "plugin_action"
command = "shelf.restore"
description = "restore an archived tab"
```

`prefix+shift+s` is unbound in herdr's default keymap (`prefix+s` is
settings). Many macOS terminals turn alt chords into characters instead of
sending them as key events, so `prefix+alt+...` bindings often do nothing
there.

## It starts in dry-run

Out of the box Shelf only reports. Each sweep shows a notification like
`shelf (dry-run): would archive 3 tabs: fix-retries, docs-pass, perf-probe`.
When the list looks right, turn archiving on in `config.json` (below) with
`"mode": "live"`.

## What gets archived

A tab is archived when all of these hold:

- it has at least one agent pane;
- every agent pane has a session id reported by herdr's official integration for
  that agent, and no agent activity for `idle_days` (default 7);
- nothing in the tab is `working`;
- it is not the focused tab.

A blocked agent (waiting on you) follows the same rule. Tabs with no agent are
never touched. Shell panes inside an archived agent tab come back as shells in
the same directory.

Sweeps run at herdr startup and on focus changes, at most once per
`sweep_interval_minutes` (default 60).

## How activity is measured

For every agent herdr detects, Shelf records activity when the agent goes to
`working`, `blocked` or `done`. Looking at a finished agent (`idle`) does not
count.

Starting or resuming an agent in a pane also counts as activity -- for example
resuming an old conversation by hand with `claude --resume <id>`, or an agent
that restarts on its own -- even though that alone sends no message and would
otherwise leave the session looking exactly as idle as before. This survives
herdr restarting too: it is remembered against the conversation itself, not
just the pane it happened in.

For Claude Code and Codex it also reads the agent's own session files, so tabs
that were already inactive before you installed Shelf can qualify on the first
sweep. For other agents the clock starts when Shelf first sees the session, so
their tabs qualify no earlier than `idle_days` after install.

## Restore

Press your picker key. The popup lists archived tabs, newest first, as many
as fit the popup. When more archived tabs exist than fit, restore the rest
with `python3 -m shelf restore <id>` (ids from `python3 -m shelf list`). Type
a number to restore one, `d <number>` to delete one, `q` to close, or Esc
then Enter to close. Deleting asks for confirmation (`[y/N]`, default no) and
is permanent: there is no undo.

A restored tab goes back to the workspace with the same name (recreated if it
is gone) with the same splits, labels and directories. Each agent is started
again with its original command line plus that agent's resume arguments, so
the conversation continues. When the agent exits you are left at a shell.

Claude Code deletes conversation files 30 days after they were last written
(`cleanupPeriodDays`). Shelf keeps a copy of each archived Claude conversation
and puts it back on restore if Claude has removed it. For other agents, their
own retention applies.

## Supported agents

Every agent herdr can resume after a restart:

claude, codex, copilot, devin, droid, kimi, mastracode, pi, omp, hermes,
opencode, qodercli, qwen, kilo, cursor, agy, grok, letta.

The resume arguments mirror herdr's own table. To change one, or add an agent,
use `agents` in `config.json`.

## Configuration

`config.json` in the directory printed by
`herdr plugin config-dir shelf`. Every key is optional. Changes take
effect the next time a hook runs (the next status change, focus change, or
sweep); there is nothing to reload.

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
- `keep_transcripts`: keep copies of Claude conversation files in the archive.
- `sessions`: which herdr sessions Shelf acts in. See "Herdr sessions" below.
- `agents.<name>.program`: executable to look for and to run.
- `agents.<name>.resume`: resume arguments; `{id}` becomes the session id.
- `agents.<name>.strip` / `strip_bare`: flags (with or without a value) removed
  from the saved command line before the resume arguments are added.
- `agents.<name>.strip_subcommand`: a subcommand plus its following argument,
  removed from the saved command line the same way (codex's own `resume <id>`
  is stripped this way before Shelf's own resume arguments are added).
- `agents.<name>.relaunch`: `"plain"` runs `<program> <resume args>` instead of
  the saved command line. Use it for an agent you start with a prompt argument,
  such as `claude "fix the build"`, or the prompt would be sent again.

## Herdr sessions

herdr plugins are installed once per machine, and every herdr session's server
loads them -- the default session, and any named session, each with its own
socket (the default session's is `herdr.sock` in herdr's config directory; a
named session `<name>`'s is `sessions/<name>/herdr.sock`). Shelf's plugin
state and config are shared across sessions, keyed only by its plugin id, so
without an allowlist Shelf would sweep every herdr session on the machine.

`sessions` in `config.json` is the allowlist: a non-empty list of session
names Shelf is allowed to act in. It defaults to `["default"]`, so Shelf
only ever touches the default session unless you add more. `"*"` allows
every session. A session not in the list gets no hooks, no sweeps, and no
manual command: `track`, `sweep --if-due` and the startup sweep return
immediately without writing anything; `open-picker`, and the manual
`sweep` (the `sweep-now` action), show a notification saying so instead of
sweeping or opening the popup; and the manual `sweep`, `archive`, `list`
and `restore` commands print that the session is not enabled and exit 1.
`pick` instead shows that same message in its own popup and waits for
Enter, like its other error screens, then exits 0 -- it is a popup pane,
not a script whose exit code anything checks.

Add a named session to `sessions` if you want Shelf to manage tabs there too.
Leave out any session whose agents are managed by another tool -- for
example a session a separate automation tool runs its own agents in --
since archiving one of its tabs would take that tab away from the tool
managing it, out from under it.

Migration to per-session state (see "Where things live" below) runs
regardless of `sessions`: it is a one-time, shared, root-level cleanup, not
tied to any one session's allowlist, so it also runs -- and logs to the
shared `shelf.log` -- from a disabled session's hooks.

Each herdr session keeps its own archive, activity history and sweep
schedule; see "Where things live" below.

## Command line

For routine use, prefer the herdr actions `shelf.sweep-now` and
`shelf.restore` over the raw commands below.

To run a command by hand, find the plugin's directory with
`herdr plugin list --plugin shelf --json` (the `plugin_root` field)
and run from there, so `python3 -m shelf` finds the `shelf` package. The CLI
looks up the same state and config directories the plugin itself uses (the
same environment variables when herdr sets them, the same defaults
otherwise), so it sees the same archived tabs and the same `config.json`:

```
python3 -m shelf sweep                 run a sweep now
python3 -m shelf archive <tab-id>      archive one tab now, ignoring idle_days and mode
python3 -m shelf list                  list archived tabs with their ids
python3 -m shelf restore <archive-id>  restore one archived tab
```

Tab ids for `archive` come from `herdr tab list`.

`shelf.log` in the plugin's state directory is the durable log.
`herdr plugin log list --plugin shelf` also shows recent plugin
output, but herdr keeps that log in memory and it is short-lived, so
`shelf.log` is the one to check for anything older than the last few
commands.

## Where things live

State: `$XDG_STATE_HOME/herdr/plugins/shelf`, or
`~/.local/state/herdr/plugins/shelf` when `XDG_STATE_HOME` is not set.
`shelf.log` (the durable log, rotated to `shelf.log.1` past 1MB) lives directly
there. Everything specific to one herdr session -- `archive/` (one folder per
archived tab), `activity.json`, and the sweep schedule -- lives under
`sessions/<name>/`, one subdirectory per herdr session listed in `sessions`
(`sessions/default/` for the default session).

**Migrating from before the allowlist.** Versions before 0.3.0 kept
`archive/`, `activity.json`, `last_sweep` and `installed_at` directly at the
state root, shared by every herdr session that ran Shelf. On upgrading,
those are moved into `sessions/default/` automatically -- merged in, never
overwritten, and self-healing if a run is interrupted or a version is rolled
back and used before upgrading again. An archived tab whose id happens to
collide with one already migrated is moved aside to
`archive.conflict/<id>` at the state root if it differs (or simply dropped
if it is an exact duplicate), left there for you to look at by hand; Shelf
itself never reads that folder again. Since every pre-0.3.0 session's
archives land in the same `sessions/default/`, removing `"default"` from
`sessions` hides all of them (they are still on disk, just not listed or
restorable until `"default"` is back in `sessions`). To roll back to a
version before 0.3.0, move the contents of `sessions/default/` back up to
the state root before installing the older version, since it only looks
there.

While that migration is still in progress -- normally an instant, but it can
be held up if an older version is still running somewhere -- `sweep` and
`archive` refuse rather than act on what might be an incomplete activity
history: hooks skip silently, and the manual commands print `shelf:
migrating state from an older version; try again in a moment` and exit 1.
`list` keeps working throughout, since it only reads.

Config: the directory printed by `herdr plugin config-dir shelf`, shared by
every herdr session.

## Uninstall

Restore anything you want to keep first: an uninstall does not bring archived
tabs back on its own.

```sh
herdr plugin uninstall shelf
```

Remove the `[[keys.command]]` block added in Install from
`~/.config/herdr/config.toml`, then run `herdr server reload-config`.

Uninstalling keeps the state and config directories, including any archived
Claude conversation copies. Delete them yourself (see Where things live
above) if you want everything gone.

## Limits

- A restore is a rebuild. Running processes, scrollback and unsaved editor state
  are not kept.
- Agents without a session id from herdr cannot be resumed, so their tabs are
  never archived.
- A conversation open in two tabs at once (the same session resumed twice) is
  left alone by the sweep, rather than risk duplicating it into two tabs;
  close one of the tabs first. A manual `archive <tab-id>` archives it anyway
  (with a warning), since it is a single, explicit action on one tab, but
  restoring the resulting archive entry will then refuse until the other
  live copy is closed.
- There is no pin or snooze yet. A tab you want to keep can be restored in one
  key press.
- A one-word prompt given on the agent's command line (`claude hello`) cannot be
  told apart from a flag value and is sent again on restore. Prompts with spaces
  are detected and fall back to a plain resume. Set `"relaunch": "plain"` for an
  agent you start this way.
- Codex command lines with options between `resume` and the session id
  (`codex resume --all ID`), or started with `codex fork`, do not restore
  cleanly; use `"relaunch": "plain"` for codex if you launch it that way.
- Tabs are matched to workspaces by name. If the workspace was renamed, the tab
  comes back in a new workspace with the old name.
- The original launch flags are lost, and the tab comes back with a plain resume
  instead, when the agent runs under a wrapper process Shelf cannot match against
  the agent table's program name (for example `node .../cli.js`), or when herdr
  itself already resumed the tab once after a server restart -- herdr's own
  resume is always plain, so the flags were already gone by the time Shelf saw
  the pane.
- Archiving a workspace's only tab closes the workspace along with it; restoring
  that tab recreates the workspace.
- History sources and transcript copies (Claude, Codex) use the herdr server's
  own `CLAUDE_CONFIG_DIR` / `CODEX_HOME` environment, not necessarily the shell's.
- Every command except `list` needs `HERDR_SOCKET_PATH`, so it must run inside a
  herdr pane (or with that variable set by hand); `list` only reads the archive
  directory and works anywhere.
- Archives are kept until you restore or delete them. There is no automatic
  expiry.
- herdr 0.9.0's tab and pane ids are positional, so a tab closing elsewhere in
  the instant between Shelf's last check and its own `tab.close` could in
  principle shift ids onto a different tab; Shelf checks for this and skips
  rather than closing the wrong tab, but herdr 0.9.1 or newer allocates stable
  ids and removes the possibility entirely.

## Development

```sh
python3 -m unittest discover -s tests -t . -v
herdr plugin link .
```

See CONTRIBUTING.md.

## License

MIT
