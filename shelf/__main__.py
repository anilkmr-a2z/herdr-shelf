"""Command-line entry: python3 -m shelf <command>."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from . import activity, agents, archive, config, picker, restore, session, sweep
from .api import Client, HerdrError
from .util import FileLock, LockBusy, now

PLUGIN_ID = "shelf"
ALWAYS_HOOKS = ("track", "open-picker")
USAGE = ("usage: python3 -m shelf {track | sweep [--if-due] | archive <tab-id> | open-picker | pick | "
         "list | restore <archive-id>}")
FMT = logging.Formatter("%(asctime)s %(levelname)s [%(session)s] %(message)s")
CONFIG_ERROR_NOTIFY_INTERVAL_SECONDS = 3600

# Pre-0.3.0 files that used to live directly under the plugin's state root,
# one per herdr session now. Migrated once into sessions/default/ -- see
# _migrate_to_sessions.
_LEGACY_SESSION_FILES = ("activity.json", "archive", "last_sweep", "installed_at")

log = logging.getLogger("shelf")


class _SessionFilter(logging.Filter):
    """Attaches the herdr session name to every log record, for FMT's %(session)s."""

    def __init__(self, session_name: str):
        super().__init__()
        self.session_name = session_name

    def filter(self, record: logging.LogRecord) -> bool:
        record.session = self.session_name
        return True


def _is_hook(command: str, args: list) -> bool:
    if command in ALWAYS_HOOKS:
        return True
    if command == "sweep":
        return "--if-due" in args
    return False


def _state_dir() -> Path:
    env = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if env:
        return Path(env)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "herdr" / "plugins" / PLUGIN_ID


def _config_dir() -> Path:
    env = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "herdr" / "plugins" / "config" / PLUGIN_ID


def _session_name() -> str:
    return session.herdr_session_name(os.environ.get("HERDR_SOCKET_PATH"))


def _session_dir(root: Path, session_name: str) -> Path:
    return root / "sessions" / session_name


def _migrate_to_sessions(root: Path) -> None:
    """One-time move of pre-0.3.0 root-level state into sessions/default/.

    Before per-session state, activity.json, archive/, last_sweep and
    installed_at lived directly under the plugin's state root. If
    "sessions" doesn't exist yet and any of those do, they are moved into
    sessions/default/ under a root lock, so two processes racing on the
    first post-upgrade invocation do not both try it. A fresh install (none
    of those files exist) leaves no trace: "sessions" is created lazily by
    whichever component needs it, not by this check, so a fresh install
    never gets an empty "sessions" directory from this alone.
    """
    sessions_dir = root / "sessions"
    if sessions_dir.exists():
        return
    with FileLock(root / "migrate.lock"):
        if sessions_dir.exists():
            return
        found = [name for name in _LEGACY_SESSION_FILES if (root / name).exists()]
        if not found:
            return
        default_dir = sessions_dir / "default"
        default_dir.mkdir(parents=True, exist_ok=True)
        for name in found:
            os.replace(str(root / name), str(default_dir / name))
        log.info("migrated legacy state into sessions/default: %s", ", ".join(found))


def _gate_config(config_dir) -> dict:
    """Config for the per-session allowlist check, loaded quietly.

    Loaded with warn=False so the "ignoring unknown key(s)" warning does not
    fire on every hook invocation. An invalid config.json falls back to the
    default sessions list for this check alone; a command that goes on to
    load config the normal way still raises ConfigError -- and, for a hook,
    still notifies through the usual rate-limited path -- exactly as before
    this check existed.
    """
    try:
        return config.load(config_dir, warn=False)
    except config.ConfigError:
        return config.DEFAULTS


def _disabled_message(name: str) -> str:
    return f"shelf is not enabled for herdr session '{name}'; add it to \"sessions\" in config.json"


def _rotate_if_large(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > 1_000_000:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


def _setup_logging(state: Path, session_name: str) -> None:
    handlers = []
    try:
        state.mkdir(parents=True, exist_ok=True)
        log_path = state / "shelf.log"
        _rotate_if_large(log_path)
        file_handler = logging.FileHandler(log_path, delay=True, encoding="utf-8")
        file_handler.setFormatter(FMT)
        handlers.append(file_handler)
    except OSError:
        pass  # no writable state dir yet; fall back to stderr only
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(FMT)
    handlers.append(stderr_handler)
    for old in log.handlers:
        old.close()
    log.handlers[:] = handlers
    log.filters[:] = [_SessionFilter(session_name)]
    log.setLevel(logging.INFO)
    log.propagate = False


def _notify_config_error(state: Path, message: str) -> None:
    """At most one 'config.json is invalid' notification per hour.

    The check-and-touch is guarded by a lock so two processes racing on the
    same broken config (e.g. a hook and a manual sweep) do not both notify;
    a process that cannot get the lock skips silently rather than waiting.
    The marker is only touched once notification.show actually succeeds, so
    a herdr-unavailable failure is not mistaken for a delivered notification
    and is retried on the next occurrence instead of being rate-limited.
    """
    try:
        with FileLock(state / "config-error.lock"):
            marker = state / "config-error-notified"
            try:
                if marker.exists() and time.time() - marker.stat().st_mtime < CONFIG_ERROR_NOTIFY_INTERVAL_SECONDS:
                    return
            except OSError:
                pass
            try:
                Client().call("notification.show",
                              {"title": "shelf", "body": f"shelf: config.json is invalid: {message}"})
            except HerdrError as e:
                log.warning("notification failed: %s", e)
                return
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch(exist_ok=True)
            except OSError:
                pass
    except LockBusy:
        pass  # another process is already handling this config error


def _describe(rec: dict, moment) -> str:
    tab = rec.get("tab", {}).get("label") or "(unnamed)"
    workspace = rec.get("workspace", {}).get("label")
    names = picker.agent_names(rec)
    days = picker.days_idle(rec, moment)
    idle = f"{days}d idle" if days is not None else ""
    parts = [tab]
    parts.extend(p for p in (workspace, names, idle) if p)
    return "  ".join(parts)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else ""
    args = argv[1:]
    state = _state_dir()
    session_name = _session_name()
    is_hook = _is_hook(command, args)
    try:
        _setup_logging(state, session_name)
        _migrate_to_sessions(state)
        return _dispatch(command, args, state)
    except config.ConfigError as e:
        log.error("%s: %s", command or "shelf", e)
        if is_hook or command == "sweep":
            _notify_config_error(state, str(e))
        return 0 if is_hook else 1
    except LockBusy as e:
        if command in ("restore", "archive"):
            # A user-facing, one-line reason to try again, rather than the
            # lock file's path.
            print("shelf: a sweep is running; try again in a moment", file=sys.stderr)
        else:
            log.error("%s: %s", command or "shelf", e)
        return 0 if is_hook else 1
    except (HerdrError, archive.Skip) as e:
        log.error("%s: %s", command or "shelf", e)
        return 0 if is_hook else 1
    except (KeyError, ValueError, OSError):
        log.exception("%s failed", command or "shelf")
        return 0 if is_hook else 1
    except Exception:
        if not is_hook:
            raise
        log.exception("%s failed", command)
        return 0  # hooks never fail loudly inside herdr


def _pick(state: Path) -> int:
    """The popup's own command. Any failure prints and waits so the popup does
    not just vanish; EOFError/KeyboardInterrupt (the user closing it) exit quietly.
    """
    try:
        arch = archive.Archive(state)
        store = activity.ActivityStore(state)
        client = Client()

        def do_restore(archive_id):
            # Loaded lazily, only when a restore is actually requested, so a
            # broken config.json does not stop the user from browsing or
            # deleting entries.
            cfg = config.load(_config_dir())
            table = agents.table(cfg["agents"])
            return restore.restore(client, arch, store, archive_id, table, now())

        def notify(title, body):
            try:
                client.call("notification.show", {"title": title, "body": body})
            except HerdrError as e:
                log.warning("notification failed: %s", e)

        picker.run(arch, do_restore, now, notify=notify)
    except (EOFError, KeyboardInterrupt):
        pass
    except Exception as e:
        log.exception("pick failed")
        print(f"shelf: {e}", file=sys.stderr)
        try:
            input("Press Enter to close. ")
        except (EOFError, KeyboardInterrupt):
            pass
    return 0


def _pick_disabled(session_name: str) -> int:
    print(_disabled_message(session_name), file=sys.stderr)
    try:
        input("Press Enter to close. ")
    except (EOFError, KeyboardInterrupt):
        pass
    return 0


def _dispatch(command: str, args: list, state: Path) -> int:
    session_name = _session_name()
    session_state = _session_dir(state, session_name)
    _allowed = {}

    def allowed() -> bool:
        if "v" not in _allowed:
            _allowed["v"] = config.session_enabled(_gate_config(_config_dir()), session_name)
        return _allowed["v"]

    if command == "track":
        if not allowed():
            return 0
        activity.track(Client(), activity.ActivityStore(session_state), os.environ.get("HERDR_PLUGIN_EVENT_JSON"),
                       os.environ.get("HERDR_PANE_ID"), now(), os.environ.get("HERDR_PLUGIN_EVENT"))
        return 0
    if command == "sweep":
        if_due = "--if-due" in args
        if if_due and not sweep.is_due(session_state, config.DEFAULTS["sweep_interval_minutes"], now()):
            # Checked before config is even loaded, using the default
            # interval: config.load's own "unknown key(s)" warning must not
            # fire on every hook invocation (startup, every focus change)
            # when a sweep is not due anyway. sweep.run() re-checks with the
            # real configured interval once it does load config below.
            return 0
        if not allowed():
            if if_due:
                return 0
            print(_disabled_message(session_name), file=sys.stderr)
            return 1
        cfg = config.load(_config_dir())
        client = Client()
        report = sweep.run(client, cfg, session_state, agents.table(cfg["agents"]), if_due=if_due)
        if report is None:
            # For a manual sweep (no --if-due), run() returning None can only
            # mean the lock was busy: the due check itself is only consulted
            # with --if-due, where nothing needs to be printed at all.
            if not if_due:
                print("shelf: another sweep is running")
            return 0
        if not if_due:
            text = sweep.summary(report)
            print(text or "shelf: nothing to archive")
            if not text:
                try:
                    client.call("notification.show", {"title": "shelf", "body": "shelf: nothing to archive"})
                except HerdrError as e:
                    log.warning("notification failed: %s", e)
        return 0
    if command == "open-picker":
        client = Client()
        if not allowed():
            try:
                client.call("notification.show",
                            {"title": "shelf", "body": f"shelf is not enabled for herdr session {session_name}"})
            except HerdrError as e:
                log.warning("notification failed: %s", e)
            return 0
        try:
            client.call("plugin.pane.open", {"plugin_id": os.environ.get("HERDR_PLUGIN_ID") or PLUGIN_ID,
                                              "entrypoint": "picker"})
        except HerdrError as e:
            if e.code == "ui_busy":
                try:
                    client.call("notification.show",
                                {"title": "shelf", "body": "shelf: close the open popup or dialog first"})
                except HerdrError:
                    pass
                return 0
            raise
        return 0
    if command in ("archive", "restore") and not args:
        print(USAGE, file=sys.stderr)
        return 2
    if command == "list":
        if not allowed():
            print(_disabled_message(session_name), file=sys.stderr)
            return 1
        arch = archive.Archive(session_state)
        records = arch.list()
        if not records:
            print("No archived tabs.")
        for rec in records:
            print(f"{rec['id']}  {_describe(rec, now())}")
        return 0
    if command == "archive":
        if not allowed():
            print(_disabled_message(session_name), file=sys.stderr)
            return 1
        cfg = config.load(_config_dir())
        table = agents.table(cfg["agents"])
        print(sweep.archive_now(Client(), cfg, session_state, table, args[0]))
        return 0
    if command == "restore":
        if not allowed():
            print(_disabled_message(session_name), file=sys.stderr)
            return 1
        arch = archive.Archive(session_state)
        try:
            arch.load(args[0])  # existence check only; any other error follows the normal path
        except KeyError:
            print(f"no archived tab '{args[0]}'; see `python3 -m shelf list`", file=sys.stderr)
            return 1
        cfg = config.load(_config_dir())
        table = agents.table(cfg["agents"])
        store = activity.ActivityStore(session_state)
        result = restore.restore(Client(), arch, store, args[0], table, now())
        for warning in result["warnings"]:
            print(warning)
        print(result["tab_id"] or "")
        return 0
    if command == "pick":
        if not allowed():
            return _pick_disabled(session_name)
        return _pick(session_state)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
