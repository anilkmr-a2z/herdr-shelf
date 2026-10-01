# Changelog

## 0.4.0 - 2026-09-30

- Redesign the restore picker as an arrow-key popup. Archived tabs are
  grouped by when they were archived: Archived today, Last 7 days, Last 30
  days, and Older, which starts collapsed. The list shows 10 rows and
  scrolls, so every entry is reachable from the popup. Enter restores,
  Right/Left open and close a group, `/` filters by tab, workspace or agent
  name, `d` deletes after a `[y/N]` confirmation, and one press of q or Esc
  closes it. A details line shows the highlighted tab's directory, when it
  was shelved, and its panes. Clicks, double-clicks and the wheel work too
  (wheel-down needs an ncurses with mouse version 2).
- **Behavior change:** the number shortcuts (`3` to restore, `d 3` to delete)
  are gone, and the popup is now 18 rows tall instead of 80% of the screen.
- The picker now uses Python's built-in `curses` module.

## 0.3.0 - 2026-09-26

- Add a herdr-session allowlist: a new `"sessions"` config key (default
  `["default"]`, `"*"` for every session) controls which herdr sessions
  Shelf acts in. Each herdr session (the default one, and any named session
  under `sessions/<name>`) has its own socket, but plugin state and config
  are shared across all of them, keyed only by plugin id; without this,
  Shelf would sweep every herdr session on the machine, including one
  driven by another tool (for example an automation session whose agents
  are managed elsewhere, where archiving a tab would take it away from that
  tool). **Behavior change:** before this release Shelf swept every herdr
  session; from this release it sweeps only the `default` session unless
  `sessions` is configured otherwise. A herdr session not in `sessions`
  gets no hooks, no sweeps, and no manual command: `track`, `sweep
  --if-due`, and the startup sweep return immediately and write nothing of
  their own (the one-time state migration below is a separate, shared
  concern and may still run from any session's hook, disabled or not);
  `open-picker`, and the manual `sweep` (the `sweep-now` action), show a
  notification instead of opening the popup or sweeping; the manual
  `sweep`, `archive`, `list` and `restore` commands print that the session
  is not enabled and exit 1; `pick` shows that message in its own popup and
  waits for Enter, exiting 0 like its other error screens. A socket path
  that clearly names a session but from which no valid name can be parsed
  is always disabled, never treated as `"default"`.
- State is now kept separately per herdr session, under
  `sessions/<name>/` in the plugin's state directory (`archive/`,
  `activity.json`, `activity.lock`, `last_sweep`, `sweep.lock`,
  `installed_at`); `shelf.log`, `shelf.log.1`, `config-error-notified` and
  `config-error.lock` stay at the state directory's root. State from
  before this release is merged into `sessions/default/` automatically --
  self-healing, so it keeps retrying on later commands rather than assuming
  one pass finished the job, and safe against a downgrade-then-upgrade
  round trip. This runs for every herdr session, including a disabled one,
  since it is a shared, one-time cleanup rather than a per-session
  concern; pre-0.3.0 archives from any herdr session all land in
  `sessions/default/`, so they are hidden if `"default"` is ever removed
  from `sessions`. An archive id colliding with one already migrated is
  moved aside to `archive.conflict/<id>` at the state root if it differs
  from the destination, or simply dropped if the two are byte-identical.
  If the migration cannot fully clear `activity.json`, `installed_at` or
  `last_sweep` from the root (for example an older version is still
  running and holds the locks it needs), `sweep` and `archive` refuse
  rather than act on what could be an incomplete activity history --
  hooks skip silently, the manual commands print `shelf: migrating state
  from an older version; try again in a moment` and exit 1; `list` keeps
  working throughout.
- `shelf.log` lines now include the herdr session name, for example
  `... INFO [default] archived old`.
- New archive records carry an informational `"herdr_session"` field
  naming the herdr session they were archived from.

## 0.2.1 - 2026-09-25

- Fix: starting or resuming an agent in a pane now counts as activity (a new
  `pane.agent_detected` hook), so a conversation resumed by hand no longer
  looks idle just because starting it sent no user or assistant message.
  This survives herdr restarting (which assigns the pane a new terminal id):
  it is remembered against the session itself, not just the pane. herdr
  often has not yet learned the session id itself at that point (Claude's
  session, in particular, comes from its own startup hook shortly after);
  `pane.get` is retried briefly, waiting for a session whose agent matches
  before giving up, and a stale session left over from a different agent is
  never marked as this one's activity.
- Fix: a conversation open in two tabs at once is no longer archived or
  restored twice. A sweep leaves both tabs alone when the same session is
  open in more than one of them (including two panes of one tab), rechecking
  right before it would close either one, and restore refuses (keeping the
  archive entry) when the conversation is already open in a live pane. A
  manual `archive <tab-id>` still proceeds in this case, with a warning,
  since it targets one tab explicitly.

## 0.2.0 - 2026-09-25

- The plugin id changed from `anilkmr.shelf` to `shelf`. The install source
  (`anilkmr-a2z/herdr-shelf`) is unchanged; only the id herdr uses to key
  state, config, and actions is different.
- Upgrading from 0.1.0:
  1. `herdr plugin uninstall anilkmr.shelf`
  2. Optionally move `~/.local/state/herdr/plugins/anilkmr.shelf` and
     `~/.config/herdr/plugins/config/anilkmr.shelf` to the corresponding
     `.../shelf` paths to keep your archives and `config.json`.
  3. `herdr plugin install anilkmr-a2z/herdr-shelf`
  4. Change the keybinding command in `~/.config/herdr/config.toml` from
     `anilkmr.shelf.restore` to `shelf.restore`, then
     `herdr server reload-config`.

## 0.1.0 - 2026-09-25

- First release: archive agent tabs after `idle_days` without agent activity,
  restore them from a popup picker with the conversation resumed.
