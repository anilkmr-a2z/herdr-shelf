"""Command-line entry: python3 -m shelf <command>."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from . import activity, agents, archive, config, picker, restore, sweep
from .api import Client, HerdrError
from .util import LockBusy, now, parse_iso

PLUGIN_ID = "anilkmr.shelf"
ALWAYS_HOOKS = ("track", "open-picker")
USAGE = ("usage: python3 -m shelf {track | sweep [--if-due] | archive <tab-id> | open-picker | pick | "
         "list | restore <archive-id>}")
FMT = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
CONFIG_ERROR_NOTIFY_INTERVAL_SECONDS = 3600
log = logging.getLogger("shelf")


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


def _rotate_if_large(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > 1_000_000:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


def _setup_logging(state: Path) -> None:
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
    log.setLevel(logging.INFO)
    log.propagate = False


def _notify_config_error(state: Path, message: str) -> None:
    """At most one 'config.json is invalid' notification per hour."""
    marker = state / "config-error-notified"
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < CONFIG_ERROR_NOTIFY_INTERVAL_SECONDS:
            return
    except OSError:
        pass
    try:
        Client().call("notification.show", {"title": "shelf", "body": f"shelf: config.json is invalid: {message}"})
    except HerdrError as e:
        log.warning("notification failed: %s", e)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    except OSError:
        pass


def _describe(rec: dict, moment) -> str:
    tab = rec.get("tab", {}).get("label") or "(unnamed)"
    workspace = rec.get("workspace", {}).get("label")
    agent_names = ",".join(sorted({m["agent"] for m in rec.get("panes", {}).values() if m.get("agent")}))
    stamps = [parse_iso(m.get("last_activity")) for m in rec.get("panes", {}).values()]
    stamps = [s for s in stamps if s is not None]
    idle = f"{(moment - max(stamps)).days}d idle" if stamps else ""
    parts = [tab]
    parts.extend(p for p in (workspace, agent_names, idle) if p)
    return "  ".join(parts)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else ""
    args = argv[1:]
    state = _state_dir()
    is_hook = _is_hook(command, args)
    try:
        _setup_logging(state)
        return _dispatch(command, args, state)
    except config.ConfigError as e:
        log.error("%s: %s", command or "shelf", e)
        if is_hook:
            _notify_config_error(state, str(e))
        return 0 if is_hook else 1
    except (HerdrError, archive.Skip, LockBusy) as e:
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
        print(f"shelf: {e}", file=sys.stderr)
        try:
            input("Press Enter to close. ")
        except (EOFError, KeyboardInterrupt):
            pass
    return 0


def _dispatch(command: str, args: list, state: Path) -> int:
    if command == "track":
        activity.track(Client(), activity.ActivityStore(state), os.environ.get("HERDR_PLUGIN_EVENT_JSON"),
                       os.environ.get("HERDR_PANE_ID"), now())
        return 0
    if command == "sweep":
        if_due = "--if-due" in args
        cfg = config.load(_config_dir())
        client = Client()
        report = sweep.run(client, cfg, state, agents.table(cfg["agents"]), if_due=if_due)
        if report is not None and not if_due:
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
        try:
            client.call("plugin.pane.open", {"plugin_id": os.environ.get("HERDR_PLUGIN_ID") or PLUGIN_ID,
                                              "entrypoint": "picker"})
        except HerdrError as e:
            if e.code == "ui_busy":
                try:
                    client.call("notification.show", {"title": "shelf", "body": "shelf: close the open popup first"})
                except HerdrError:
                    pass
                return 0
            raise
        return 0
    if command in ("archive", "restore") and not args:
        print(USAGE, file=sys.stderr)
        return 2
    if command == "list":
        arch = archive.Archive(state)
        records = arch.list()
        if not records:
            print("No archived tabs.")
        for rec in records:
            print(f"{rec['id']}  {_describe(rec, now())}")
        return 0
    if command == "archive":
        cfg = config.load(_config_dir())
        table = agents.table(cfg["agents"])
        print(sweep.archive_now(Client(), cfg, state, table, args[0]))
        return 0
    if command == "restore":
        cfg = config.load(_config_dir())
        table = agents.table(cfg["agents"])
        arch = archive.Archive(state)
        store = activity.ActivityStore(state)
        try:
            result = restore.restore(Client(), arch, store, args[0], table, now())
        except KeyError:
            print(f"no archived tab '{args[0]}'; see `python3 -m shelf list`", file=sys.stderr)
            return 1
        for warning in result["warnings"]:
            print(warning)
        print(result["tab_id"] or "")
        return 0
    if command == "pick":
        return _pick(state)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
