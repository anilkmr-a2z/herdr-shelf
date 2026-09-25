"""Load config.json from the plugin's config directory."""

from __future__ import annotations

import copy
import json
from pathlib import Path

DEFAULTS = {
    "idle_days": 7,
    "mode": "dry-run",
    "sweep_interval_minutes": 60,
    "keep_transcripts": True,
    "agents": {},
}


class ConfigError(Exception):
    pass


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load(config_dir) -> dict:
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
    for key in DEFAULTS:
        if key in raw:
            cfg[key] = raw[key]
    _validate(cfg, path)
    return cfg


def _validate(cfg: dict, path: Path) -> None:
    if not _is_number(cfg["idle_days"]) or cfg["idle_days"] <= 0:
        raise ConfigError(f"{path}: idle_days must be a positive number")
    if cfg["mode"] not in ("dry-run", "live"):
        raise ConfigError(f'{path}: mode must be "dry-run" or "live"')
    if not _is_number(cfg["sweep_interval_minutes"]) or cfg["sweep_interval_minutes"] < 0:
        raise ConfigError(f"{path}: sweep_interval_minutes must be zero or more")
    if not isinstance(cfg["keep_transcripts"], bool):
        raise ConfigError(f"{path}: keep_transcripts must be true or false")
    agents = cfg["agents"]
    if not isinstance(agents, dict) or not all(isinstance(v, dict) for v in agents.values()):
        raise ConfigError(f"{path}: agents must map agent names to objects")
