# AGENTS.md

Guidance for coding agents and humans working on herdr-shelf.

## What this is

herdr-shelf ("Shelf") is a stdlib-only Python 3.9+ plugin for the herdr
terminal workspace manager. It archives agent tabs whose agents have been
inactive for `idle_days` (default 7), and restores an archived tab later with
its layout, working directories and agent conversation resumed. See
`README.md` for user-facing behavior and `docs/superpowers/specs/2026-09-24-herdr-shelf-design.md`
for the full design (activity model, eligibility rules, state layout).

## Module map

- `shelf/__init__.py`: package marker, `__version__`.
- `shelf/__main__.py`: CLI entry point (`track`, `sweep [--if-due]`,
  `archive <tab-id>`, `open-picker`, `pick`, `list`, `restore <id>`). Runs the
  migration first, then gates every command on the herdr session allowlist.
- `shelf/api.py`: minimal client for herdr's local socket (one JSON request
  per line). `HerdrError.definite` tells a definite refusal apart from an
  unknown outcome (timeout, bad reply).
- `shelf/agents.py`: `BUILTIN` agent table mirroring herdr's
  `src/agent_resume.rs`; builds relaunch argv, strips old resume flags,
  validates session values.
- `shelf/activity.py`: `ActivityStore` -- records "active now", `first_seen`,
  `agent_started_at`, `restored_at`; computes each session's effective
  activity.
- `shelf/history.py`: optional per-agent history readers (Claude, Codex) that
  read the agent's own session files for activity older than Shelf itself.
- `shelf/sweep.py`: eligibility (`decide()`), gathering tabs/panes, dry-run
  reporting vs. live archiving, one summary notification per sweep.
- `shelf/archive.py`: archive records -- create, list, load, delete;
  `capture()` builds a record; `archive_tab()` writes it, then closes the tab.
- `shelf/restore.py`: rebuilds a tab from an archive record and resumes its
  agents, under `sweep.lock`.
- `shelf/picker.py`: the restore popup. Pure logic (`State`, `reduce`,
  `render`, `apply`) tested without a terminal, plus a thin curses loop
  (`run`); `tests/test_picker_tty.py` drives that loop in a pseudo-terminal.
- `shelf/session.py`: `herdr_session_name()` -- recovers the herdr session
  name from `HERDR_SOCKET_PATH`.
- `shelf/migrate.py`: one-way, self-healing merge of pre-0.3.0 root-level
  state into `sessions/default/`.
- `shelf/config.py`: loads and validates `config.json`, applies defaults, and
  provides the lock-free session-allowlist check used by the gate.
- `shelf/util.py`: UTC timestamp helpers, atomic JSON writes, `FileLock`.

## Running the tests

```sh
python3 -m unittest discover -s tests -t . -v
```

CI (`.github/workflows/ci.yml`) runs this on Ubuntu and macOS under both
Python 3.9 and 3.12. If you have both interpreters locally, run the suite
under each before sending a change:

```sh
python3.9 -m unittest discover -s tests -t . -v
python3.12 -m unittest discover -s tests -t . -v
```

## Testing rules

- Never call a real herdr server from a test. Use `tests/fakeherdr.py`, a
  fake Unix-socket server: `handlers` maps a method name to a callable that
  returns a result dict or raises `FakeError(code, message)`; every request
  is recorded in `calls`.
- Always call `fake.close()` (or use it as you already do in existing
  tests) -- `close()` raises `AssertionError` if any handler raised during the
  test, so a bug inside a handler fails the test instead of passing silently.
- A new test must fail without the code change it is testing. Comment out or
  revert your fix locally and confirm the test goes red before trusting it.

## Non-negotiable invariants

These hold everywhere in the codebase; a change that violates one is a bug,
not a style choice.

- The archive record is written (`arch.save`) before `tab.close` is called,
  so a failed close never loses a tab's layout, cwd or session id.
- An archive record is deleted only when herdr definitely refused the close
  (`HerdrError.definite`) or a live re-check proves the tab is still open.
  When the outcome of `tab.close` is unknown, the record is kept rather than
  risked as the only surviving copy.
- Never archive a tab that is focused, or has any pane `working`, or whose
  conversation is open in more than one pane or more than one tab. A manual
  `archive <tab-id>` is the one exception: it proceeds anyway (with a
  warning) since it targets one tab explicitly.
- When anything about a tab is uncertain (a failed `pane.process_info`, an
  unrecognized event shape, an ambiguous id match), archive nothing for that
  tab this sweep rather than guess.
- Lock order is `sweep.lock` before `activity.lock`. herdr is never called
  while holding `activity.lock`. `sweep.lock` is held for the duration of a
  sweep, a restore, or a picker delete; `activity.lock` is held only for the
  brief read-modify-write of `activity.json`.
- Hooks (`track`, `sweep --if-due`, `open-picker`) always exit 0, even on an
  unexpected exception, so a bug in the plugin never breaks herdr itself.
- State is per herdr session, under `sessions/<name>/` in the plugin's state
  directory. Every herdr session shares one plugin install, so a session
  allowlist (`sessions` in `config.json`, default `["default"]`) gates every
  command; `shelf/session.py` recovers the session name from
  `HERDR_SOCKET_PATH` and `shelf/config.py` checks it before any state is
  touched.
- The migration from pre-0.3.0 root-level state into `sessions/default/`
  never overwrites an existing destination; a collision is left in place
  with a logged warning rather than silently discarded or renamed.

## herdr API facts that bit us

- The event JSON's own `"event"` field (and `"data"`'s `"type"`) use herdr's
  snake_case `EventKind` name, for example `"pane_agent_detected"` --
  `HERDR_PLUGIN_EVENT` uses the dot form (`"pane.agent_detected"`) instead.
  `track()` checks both, plus `data.type`, and treats any one match as
  enough.
- `layout.apply` rejects a request carrying both `tab_id` and
  `workspace_id`. Send exactly one, never both.
- herdr 0.9.0 tab and pane ids are positional, not stable, so an unrelated
  tab closing between a sweep's gather step and its own `tab.close` can shift
  ids onto a different tab. Shelf re-checks pane terminal ids right before
  closing and skips rather than closing the wrong tab.
- Closing a workspace's only tab closes the workspace along with it;
  restoring that tab recreates the workspace.
- Workspace info has no `cwd`. Shelf records the first pane's cwd as the
  workspace cwd instead.
- herdr's own resume of a pane it restored on a server restart sets the
  agent label directly while restoring it, rather than through detection, so
  it never fires `pane.agent_detected`. Do not rely on that event firing for
  every session start.

## Adding an agent

1. Add a row to `BUILTIN` in `shelf/agents.py`. It should match herdr's own
   `src/agent_resume.rs` for that agent: `program`, `resume` args (with
   `{id}`), and any `strip` / `strip_bare` / `strip_subcommand` needed to
   remove a previous resume from a saved launch command.
2. Add a matching case to `EXPECTED` in `tests/test_agents.py`.
3. A history reader in `shelf/history.py` is optional -- add one only if the
   agent has its own session files worth reading for pre-install activity.

## Release checklist

1. Bump the version in `herdr-plugin.toml` (`version`) and
   `shelf/__init__.py` (`__version__`). Keep them equal.
2. Date the new entry in `CHANGELOG.md` (move it out of "unreleased").
3. Confirm CI is green on the release commit.
4. Compare the agent table against herdr's current `src/agent_resume.rs` and
   add any new agents first.
5. Test against a real herdr server (see below), then push an annotated tag
   and create the GitHub release with `gh`.
6. Keep the `herdr-plugin` GitHub topic set, since herdr's marketplace uses
   it for discovery.

## Repo conventions

- ASCII only. No em dashes, no smart quotes; use a plain double hyphen for a
  parenthetical aside, matching the existing docs.
- Stdlib only. No third-party dependencies, ever.
- Python 3.9 compatible: no syntax or stdlib API newer than 3.9 (the repo
  uses `from __future__ import annotations` to allow newer type-hint syntax
  under 3.9).
- Inclusive language in code, comments, commits and docs.
- No private paths, hostnames, company names or internal tool names in any
  tracked file. This is a public repo listed in herdr's plugin marketplace.
- Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`,
  etc.).
- Stage files explicitly by path when committing (`git add <path>`, not
  `git add -A` or `git add .`), so an unrelated or generated file never rides
  along in a commit.

## Checking against a real herdr safely

Shelf ships in dry-run mode by default (`"mode": "dry-run"` in
`config.json`); a sweep only reports what it would archive, e.g.
`shelf (dry-run): would archive 3 tabs: ...`. Prefer this over live mode
whenever you just want to see what Shelf thinks is eligible.

When you do need to exercise live archive/restore against a real server
(required before a release):

- Use a throwaway scratch tab you started yourself, running an agent you do
  not mind losing scrollback on, rather than a tab with work you care about.
- Confirm the tab shows up in a dry-run sweep first, then switch that one
  session to `"mode": "live"` (or set it for a session name you added
  specifically for testing, via `sessions` in `config.json`) before running
  `python3 -m shelf sweep` or the `shelf.sweep-now` action.
- Restore it back with `python3 -m shelf restore <id>` (or the picker) and
  confirm the conversation resumes with its original launch flags before
  trusting the change.
