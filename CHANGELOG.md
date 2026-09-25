# Changelog

## 0.2.1 - unreleased

- Fix: starting or resuming an agent in a pane now counts as activity (a new
  `pane.agent_detected` hook), so a conversation resumed by hand no longer
  looks idle just because starting it sent no user or assistant message.
  This survives herdr restarting (which assigns the pane a new terminal id):
  it is remembered against the session itself, not just the pane.
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
