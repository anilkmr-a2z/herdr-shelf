"""Load config.json from the plugin's config directory."""

from __future__ import annotations

import copy
import json
import logging
import math
from pathlib import Path

log = logging.getLogger("shelf")

DEFAULTS = {
    "idle_days": 7,
    "mode": "dry-run",
    "sweep_interval_minutes": 60,
    "keep_transcripts": True,
    "agents": {},
    "sessions": ["default"],
}

# The value that allows every herdr session, rather than naming each one.
ALL_SESSIONS = "*"

MAX_IDLE_DAYS = 3650
MAX_SWEEP_INTERVAL_MINUTES = 10080

AGENT_KEYS = frozenset({"program", "resume", "strip", "strip_bare", "strip_subcommand", "relaunch"})


class ConfigError(Exception):
    pass


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load(config_dir, warn: bool = True) -> dict:
    """Load config.json, applying defaults for missing keys.

    warn=False suppresses the "ignoring unknown key(s)" log warnings (top
    level and per-agent), for a caller that loads config on every event (for
    example the per-session allowlist check) and must not flood shelf.log.
    Invalid values still raise ConfigError either way.
    """
    cfg = copy.deepcopy(DEFAULTS)
    if not config_dir:
        return cfg
    path = Path(config_dir) / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: the top level must be an object")
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown and warn:
        log.warning("%s: ignoring unknown key(s): %s", path, ", ".join(unknown))
    for key in DEFAULTS:
        if key in raw:
            cfg[key] = raw[key]
    _validate(cfg, path, warn)
    return cfg


def session_enabled(cfg: dict, name: str) -> bool:
    """Whether Shelf should act in the herdr session called name.

    cfg["sessions"] defaults to ["default"]; "*" in the list allows every
    session.
    """
    sessions = cfg.get("sessions") or DEFAULTS["sessions"]
    return ALL_SESSIONS in sessions or name in sessions


def _validate(cfg: dict, path: Path, warn: bool = True) -> None:
    idle_days = cfg["idle_days"]
    if not _is_number(idle_days) or not math.isfinite(idle_days) or idle_days <= 0:
        raise ConfigError(f"{path}: idle_days must be a positive number")
    if idle_days > MAX_IDLE_DAYS:
        raise ConfigError(f"{path}: idle_days must be at most {MAX_IDLE_DAYS}")
    if cfg["mode"] not in ("dry-run", "live"):
        raise ConfigError(f'{path}: mode must be "dry-run" or "live"')
    sweep = cfg["sweep_interval_minutes"]
    if not _is_number(sweep) or not math.isfinite(sweep) or sweep < 0:
        raise ConfigError(f"{path}: sweep_interval_minutes must be zero or more")
    if sweep > MAX_SWEEP_INTERVAL_MINUTES:
        raise ConfigError(f"{path}: sweep_interval_minutes must be at most {MAX_SWEEP_INTERVAL_MINUTES}")
    if not isinstance(cfg["keep_transcripts"], bool):
        raise ConfigError(f"{path}: keep_transcripts must be true or false")
    agents = cfg["agents"]
    if not isinstance(agents, dict) or not all(isinstance(v, dict) for v in agents.values()):
        raise ConfigError(f"{path}: agents must map agent names to objects")
    for name, entry in agents.items():
        _validate_agent(name, entry, path, warn)
    _validate_sessions(cfg, path)


def _is_valid_sessions_value(sessions) -> bool:
    return isinstance(sessions, list) and bool(sessions) and all(isinstance(s, str) and s for s in sessions)


def _validate_sessions(cfg: dict, path: Path) -> None:
    if not _is_valid_sessions_value(cfg["sessions"]):
        raise ConfigError(f"{path}: sessions must be a non-empty list of non-empty strings")


def sessions_for_gate(config_dir) -> list:
    """The "sessions" list to use for the per-session allowlist gate alone.

    Never raises and never logs, for a caller (the gate check in __main__)
    that runs on every hook invocation and must not flood shelf.log, and
    must still have an answer even when config.json is otherwise broken.
    If "sessions" on its own is a valid non-empty list of non-empty strings,
    it is used as-is, regardless of any other invalid value elsewhere in
    the file -- a hook still needs to know which session it may act in even
    when, say, idle_days is broken. Otherwise (a missing file, malformed
    JSON, a non-object top level, a missing "sessions" key, or an invalid
    "sessions" value) the default sessions list is used.
    """
    default = list(DEFAULTS["sessions"])
    if not config_dir:
        return default
    path = Path(config_dir) / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default
    if not isinstance(raw, dict) or "sessions" not in raw:
        return default
    sessions = raw["sessions"]
    return sessions if _is_valid_sessions_value(sessions) else default


def _is_str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _validate_agent(name: str, entry: dict, path: Path, warn: bool = True) -> None:
    unknown = sorted(set(entry) - AGENT_KEYS)
    if unknown and warn:
        log.warning("%s: agents.%s: ignoring unknown key(s): %s", path, name, ", ".join(unknown))
    if "program" in entry and not (isinstance(entry["program"], str) and entry["program"]):
        raise ConfigError(f"{path}: agents.{name}.program must be a non-empty string")
    if "resume" in entry:
        if not (_is_str_list(entry["resume"]) and entry["resume"]):
            raise ConfigError(f"{path}: agents.{name}.resume must be a non-empty list of strings")
        if not any("{id}" in item for item in entry["resume"]):
            raise ConfigError(f'{path}: agents.{name}.resume must contain "{{id}}" in at least one element')
    if "strip" in entry and not _is_str_list(entry["strip"]):
        raise ConfigError(f"{path}: agents.{name}.strip must be a list of strings")
    if "strip_bare" in entry and not _is_str_list(entry["strip_bare"]):
        raise ConfigError(f"{path}: agents.{name}.strip_bare must be a list of strings")
    if "strip_subcommand" in entry and not isinstance(entry["strip_subcommand"], str):
        raise ConfigError(f"{path}: agents.{name}.strip_subcommand must be a string")
    if "relaunch" in entry and entry["relaunch"] != "plain":
        raise ConfigError(f'{path}: agents.{name}.relaunch must be "plain"')
