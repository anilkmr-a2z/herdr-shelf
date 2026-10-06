"""Command-line entry: python3 -m shelf <command>."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import activity, agents, archive, config, confirm, migrate, picker, restore, session, sweep
from .api import Client, HerdrError
from .util import FileLock, LockBusy, now

PLUGIN_ID = "shelf"
ALWAYS_HOOKS = ("track", "open-picker", "open-archive")
USAGE = ("usage: python3 -m shelf {track | sweep [--if-due] | archive <tab-id> | open-archive | "
         "confirm-archive | open-picker | pick | list | restore <archive-id>}")
FMT = logging.Formatter("%(asctime)s %(levelname)s [%(session)s] %(message)s")
CONFIG_ERROR_NOTIFY_INTERVAL_SECONDS = 3600

# Displayed (in log lines and messages) in place of the herdr session name
# when session.herdr_session_name could not parse one at all -- see
# _session_name. Such a session is always disabled; this is display-only.
_UNKNOWN_SESSION_DISPLAY = "unknown"

# Root-level files a pre-0.3.0 process still reads and writes. If any of
# these are still at the root after a migration attempt (for example a
# still-running 0.2.x process held the root locks migrate.py needs), sweep
# and archive must not run against what could be an incomplete activity
# history. archive/ is deliberately not included: an archive id collision
# always ends up resolved (deleted if identical, moved to
# archive.conflict/<id> if not -- see shelf.migrate), and that resolution
# can legitimately be the permanent, final state, so it must never block
# sweep/archive.
_MIGRATION_MARKERS = ("activity.json", "installed_at", "last_sweep")
_MIGRATING_MESSAGE = "shelf: migrating state from an older version; try again in a moment"

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


def _session_name():
    """The herdr session this invocation runs in, or None when
    HERDR_SOCKET_PATH clearly names a session but no valid name could be
    parsed from it -- see shelf.session.herdr_session_name. None is always
    treated as disabled, regardless of "sessions" in config.json (including
    "*"): with no reliable name, there is nothing to safely enable.
    """
    return session.herdr_session_name(os.environ.get("HERDR_SOCKET_PATH"))


def _display_session_name(session_name) -> str:
    return session_name if session_name is not None else _UNKNOWN_SESSION_DISPLAY


def _session_dir(root: Path, session_name: str) -> Path:
    return root / "sessions" / session_name


def _session_allowed(session_name, config_dir) -> bool:
    if session_name is None:
        return False
    sessions = config.sessions_for_gate(config_dir)
    return config.session_enabled({"sessions": sessions}, session_name)


def _migration_incomplete(root: Path) -> bool:
    return any((root / name).exists() for name in _MIGRATION_MARKERS)


def _disabled_message(session_name) -> str:
    name = _display_session_name(session_name)
    return f"shelf is not enabled for herdr session '{name}'; add it to \"sessions\" in config.json"


def _notify(client, body: str) -> None:
    try:
        client.call("notification.show", {"title": "shelf", "body": body})
    except HerdrError as e:
        log.warning("notification failed: %s", e)


def _notify_disabled(client, session_name) -> None:
    try:
        client.call("notification.show",
                    {"title": "shelf", "body": f"shelf is not enabled for herdr session "
                                                f"{_display_session_name(session_name)}"})
    except HerdrError as e:
        log.warning("notification failed: %s", e)


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
        _setup_logging(state, _display_session_name(session_name))
        # Runs for every session, including a disabled one: it is a shared,
        # root-level concern, independent of any one session's allowlist.
        migrate.merge_into_default_session(state)
        return _dispatch(command, args, state, session_name)
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


@contextmanager
def _logs_off_stderr():
    """Keep log lines off a popup's screen while it runs: a line on stderr
    (a restore or an archive logs several) would draw over it. shelf.log
    still gets every line."""
    on_stderr = [h for h in log.handlers if type(h) is logging.StreamHandler]
    for handler in on_stderr:
        log.removeHandler(handler)
    quiet = logging.NullHandler()  # with no shelf.log, logging.lastResort would write to stderr
    log.addHandler(quiet)
    try:
        yield
    finally:
        log.removeHandler(quiet)
        for handler in on_stderr:
            log.addHandler(handler)


def _open_archive(client, session_state: Path) -> None:
    """Ask before archiving the tab herdr invoked this action for
    (HERDR_TAB_ID): a notification for a block, otherwise the popup."""
    tab_id = os.environ.get("HERDR_TAB_ID")
    if not tab_id:
        _notify(client, "shelf: no tab to archive")
        return
    cfg = config.load(_config_dir())
    found = sweep.preview(client, session_state, agents.table(cfg["agents"]), tab_id)
    if found["blocks"]:
        _notify(client, f'shelf: can\'t archive "{found["label"]}": {found["blocks"][0]}')
        return
    lines = confirm.lines(found)
    try:
        client.call("plugin.pane.open", {
            "plugin_id": os.environ.get("HERDR_PLUGIN_ID") or PLUGIN_ID, "entrypoint": "archive-confirm",
            "height": len(lines) + 3,  # the border, and a spare row for "Archiving..."
            "env": {"SHELF_TAB_ID": found["tab_id"], "SHELF_TAB_LABEL": found["label"],
                    "SHELF_TAB_TERMINALS": ",".join(found["terminals"]), "SHELF_CONFIRM_TEXT": "\n".join(lines)}})
    except HerdrError as e:
        if e.code != "ui_busy":
            raise
        _notify(client, "shelf: close the open popup or dialog first")


def _confirm_archive(client, session_state: Path, session_name: str) -> None:
    """The archive-confirm popup's own command: show the question, read one
    key, and archive on y. Every outcome is a notification, since the popup
    closes as soon as this returns."""
    label = os.environ.get("SHELF_TAB_LABEL") or "tab"
    with _logs_off_stderr():
        try:
            print(os.environ.get("SHELF_CONFIRM_TEXT", ""), flush=True)
            key = confirm.read_key(sys.stdin.fileno())
        except (EOFError, KeyboardInterrupt):
            return
        except Exception as e:
            log.exception("confirm-archive failed")
            _notify(client, f'shelf: can\'t archive "{label}": {e}')
            return
        if key not in (b"y", b"Y"):
            return
        # From here on, ignore Ctrl-C (read_key() has put the terminal back, so
        # it works again) and SIGHUP (herdr closes this popup, hanging up its
        # terminal, once the tab it was opened over is gone, which is the tab
        # being archived): either could cut the archive short or stop the
        # notification below. herdr follows SIGHUP with SIGTERM after 250 ms,
        # so the notification still races that.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        print(" Archiving...", end="", flush=True)  # a newline would scroll the question off the popup
        terminals = [t for t in os.environ.get("SHELF_TAB_TERMINALS", "").split(",") if t]
        try:
            cfg = config.load(_config_dir())
            sweep.archive_now(client, cfg, session_state, agents.table(cfg["agents"]),
                              os.environ.get("SHELF_TAB_ID", ""), herdr_session=session_name,
                              terminals=terminals, confirmed=True)
        except LockBusy:
            _notify(client, "shelf: a sweep is running; try again in a moment")
        except archive.Skip as e:
            log.info("confirm-archive: can't archive %s: %s", label, e)
            _notify(client, f'shelf: can\'t archive "{label}": {e}')
        except Exception as e:
            log.exception("confirm-archive failed")
            _notify(client, f'shelf: can\'t archive "{label}": {e}')
        else:
            _notify(client, f'shelf: archived "{label}"')


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

        with _logs_off_stderr():
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


def _pick_disabled(session_name) -> int:
    print(_disabled_message(session_name), file=sys.stderr)
    try:
        input("Press Enter to close. ")
    except (EOFError, KeyboardInterrupt):
        pass
    return 0


def _dispatch(command: str, args: list, state: Path, session_name) -> int:
    # session_state is None exactly when session_name is None (an
    # unparseable HERDR_SOCKET_PATH): allowed() is always False then, and no
    # branch below reads session_state without going through allowed()
    # first, other than sweep's own due-check, which is itself guarded on
    # session_state being set.
    session_state = _session_dir(state, session_name) if session_name is not None else None
    _allowed = {}

    def allowed() -> bool:
        if "v" not in _allowed:
            _allowed["v"] = _session_allowed(session_name, _config_dir())
        return _allowed["v"]

    if command == "track":
        if not allowed():
            return 0
        activity.track(Client(), activity.ActivityStore(session_state), os.environ.get("HERDR_PLUGIN_EVENT_JSON"),
                       os.environ.get("HERDR_PANE_ID"), now(), os.environ.get("HERDR_PLUGIN_EVENT"))
        return 0
    if command == "sweep":
        if_due = "--if-due" in args
        if if_due and session_state is not None \
                and not sweep.is_due(session_state, config.DEFAULTS["sweep_interval_minutes"], now()):
            # Checked before config is even loaded, using the default
            # interval: config.load's own "unknown key(s)" warning must not
            # fire on every hook invocation (startup, every focus change)
            # when a sweep is not due anyway. sweep.run() re-checks with the
            # real configured interval once it does load config below.
            return 0
        if _migration_incomplete(state):
            if if_due:
                log.info("sweep: migrating state from an older version; skipping this sweep")
                return 0
            print(_MIGRATING_MESSAGE, file=sys.stderr)
            return 1
        if not allowed():
            if if_due:
                return 0
            # The printed message is the guaranteed signal for a manual
            # sweep run from a terminal; the notification (like
            # open-picker's) is a best-effort addition for the sweep-now
            # action, triggered from herdr's UI rather than a terminal, so a
            # missing or unreachable HERDR_SOCKET_PATH must never keep the
            # message itself from being printed.
            print(_disabled_message(session_name), file=sys.stderr)
            try:
                _notify_disabled(Client(), session_name)
            except HerdrError:
                pass
            return 1
        cfg = config.load(_config_dir())
        client = Client()
        report = sweep.run(client, cfg, session_state, agents.table(cfg["agents"]), if_due=if_due,
                           herdr_session=session_name)
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
            _notify_disabled(client, session_name)
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
    if command == "open-archive":
        client = Client()
        if not allowed():
            _notify_disabled(client, session_name)
            return 0
        if _migration_incomplete(state):
            _notify(client, _MIGRATING_MESSAGE)
            return 0
        try:
            _open_archive(client, session_state)
        except Exception as e:
            log.exception("open-archive failed")
            _notify(client, f"shelf: can't archive this tab: {e}")
        return 0
    if command == "confirm-archive":
        client = Client()
        if not allowed():
            _notify_disabled(client, session_name)
            return 0
        if _migration_incomplete(state):
            _notify(client, _MIGRATING_MESSAGE)
            return 0
        _confirm_archive(client, session_state, session_name)
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
        if _migration_incomplete(state):
            print(_MIGRATING_MESSAGE, file=sys.stderr)
            return 1
        if not allowed():
            print(_disabled_message(session_name), file=sys.stderr)
            return 1
        cfg = config.load(_config_dir())
        table = agents.table(cfg["agents"])
        print(sweep.archive_now(Client(), cfg, session_state, table, args[0], herdr_session=session_name))
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
