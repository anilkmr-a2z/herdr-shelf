# Changelog

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
