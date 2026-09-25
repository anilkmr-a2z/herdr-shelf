"""Command-line entry: python3 -m shelf <command>."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from . import activity, agents, archive, config, picker, restore, sweep
from .api import Client, HerdrError
from .util import LockBusy, now

PLUGIN_ID = "anilkmr.shelf"
HOOKS = ("track", "sweep", "open-picker")
USAGE = ("usage: python3 -m shelf {track | sweep [--if-due] | archive <tab-id> | open-picker | pick | "
         "list | restore <archive-id>}")
log = logging.getLogger("shelf")


def _state_dir() -> Path:
    return Path(os.environ.get("HERDR_PLUGIN_STATE_DIR") or Path.home() / ".local" / "state" / "herdr-shelf")


def _config_dir():
    value = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    return Path(value) if value else None


def _setup_logging(state: Path) -> None:
    state.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(state / "shelf.log")
    file_handler.setFormatter(fmt)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    for old in log.handlers:
        old.close()
    log.handlers[:] = [file_handler, stderr_handler]
    log.setLevel(logging.INFO)
    log.propagate = False


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else ""
    state = _state_dir()
    _setup_logging(state)
    try:
        return _dispatch(command, argv[1:], state)
    except (HerdrError, config.ConfigError, archive.Skip, LockBusy, OSError, KeyError, ValueError) as e:
        log.error("%s: %s", command or "shelf", e)
        return 0 if command in HOOKS else 1
    except Exception:
        if command not in HOOKS:
            raise
        log.exception("%s failed", command)
        return 0  # hooks never fail loudly inside herdr


def _dispatch(command: str, args: list, state: Path) -> int:
    if command == "track":
        activity.track(Client(), activity.ActivityStore(state), os.environ.get("HERDR_PLUGIN_EVENT_JSON"),
                       os.environ.get("HERDR_PANE_ID"), now())
        return 0
    if command == "sweep":
        cfg = config.load(_config_dir())
        report = sweep.run(Client(), cfg, state, agents.table(cfg["agents"]), if_due="--if-due" in args)
        if report is not None and "--if-due" not in args:
            print(sweep.summary(report) or "shelf: nothing to archive")
        return 0
    if command == "open-picker":
        Client().call("plugin.pane.open", {"plugin_id": os.environ.get("HERDR_PLUGIN_ID") or PLUGIN_ID,
                                           "entrypoint": "picker"})
        return 0
    if command in ("archive", "restore") and not args:
        print(USAGE, file=sys.stderr)
        return 2
    if command in ("archive", "pick", "list", "restore"):
        cfg = config.load(_config_dir())
        table = agents.table(cfg["agents"])
        arch = archive.Archive(state)
        store = activity.ActivityStore(state)
        if command == "archive":
            print(sweep.archive_now(Client(), cfg, state, table, args[0]))
            return 0
        if command == "list":
            records = arch.list()
            if not records:
                print("No archived tabs.")
            for rec, line in zip(records, picker.render(records, now())):
                print(f"{rec['id']}  {line.strip()}")
            return 0
        if command == "restore":
            result = restore.restore(Client(), arch, store, args[0], table, now())
            for warning in result["warnings"]:
                print(warning)
            print(result["tab_id"] or "")
            return 0
        client = Client()
        try:
            picker.run(arch, lambda archive_id: restore.restore(client, arch, store, archive_id, table, now()), now)
        except (EOFError, KeyboardInterrupt):
            pass
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
