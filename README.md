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
macOS.

Add a key for the restore picker to `~/.config/herdr/config.toml`, then run
`herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+alt+s"
type = "plugin_action"
command = "anilkmr.shelf.restore"
description = "restore an archived tab"
```

`prefix+alt+s` is unbound in herdr's default keymap (`prefix+s` is settings).

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

For Claude Code and Codex it also reads the agent's own session files, so tabs
that were already inactive before you installed Shelf can qualify on the first
sweep. For other agents the clock starts when Shelf first sees the session, so
their tabs qualify no earlier than `idle_days` after install.

## Restore

Press your picker key. The popup lists archived tabs, newest first. Type a
number to restore one, `d <number>` to delete one, or `q` to close.

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
`herdr plugin config-dir anilkmr.shelf`. Every key is optional.

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
- `keep_transcripts`: keep copies of Claude conversation files in the archive.
- `agents.<name>.program`: executable to look for and to run.
- `agents.<name>.resume`: resume arguments; `{id}` becomes the session id.
- `agents.<name>.strip` / `strip_bare`: flags (with or without a value) removed
  from the saved command line before the resume arguments are added.
- `agents.<name>.relaunch`: `"plain"` runs `<program> <resume args>` instead of
  the saved command line. Use it for an agent you start with a prompt argument,
  such as `claude "fix the build"`, or the prompt would be sent again.

## Command line

Run from the plugin directory with the plugin's environment, or use the herdr
actions `anilkmr.shelf.sweep-now` and `anilkmr.shelf.restore`:

```
python3 -m shelf sweep                 run a sweep now
python3 -m shelf archive <tab-id>      archive one tab now, ignoring idle_days
python3 -m shelf list                  list archived tabs with their ids
python3 -m shelf restore <archive-id>  restore one archived tab
```

Logs go to `shelf.log` in the plugin's state directory and to
`herdr plugin log anilkmr.shelf`.

## Limits

- A restore is a rebuild. Running processes, scrollback and unsaved editor state
  are not kept.
- Agents without a session id from herdr cannot be resumed, so their tabs are
  never archived.
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

## Development

```sh
python3 -m unittest discover -s tests -t . -v
herdr plugin link .
```

See CONTRIBUTING.md.

## License

MIT
