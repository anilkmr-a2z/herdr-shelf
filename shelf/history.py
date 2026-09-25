"""Optional history sources: read an agent's own session files for its last activity."""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime
from pathlib import Path

from .util import parse_iso


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def claude_session_file(session_id: str) -> Path | None:
    pattern = str(claude_home() / "projects" / "*" / f"{glob.escape(session_id)}.jsonl")
    matches = sorted(glob.glob(pattern))
    return Path(matches[0]) if matches else None


def claude_session_paths(session_id: str) -> list[Path]:
    """The session file plus its companion directory, when those exist."""
    session_file = claude_session_file(session_id)
    if session_file is None:
        return []
    paths = [session_file]
    companion = session_file.with_suffix("")
    if companion.is_dir():
        paths.append(companion)
    return paths


def codex_session_file(session_id: str) -> Path | None:
    pattern = str(codex_home() / "sessions" / "**" / f"rollout-*-{glob.escape(session_id)}.jsonl")
    matches = sorted(glob.glob(pattern, recursive=True))
    return Path(matches[-1]) if matches else None


def _last_timestamp(path: Path, keep) -> datetime | None:
    latest = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or not keep(entry):
                continue
            ts = parse_iso(entry.get("timestamp"))
            if ts is not None and (latest is None or ts > latest):
                latest = ts
    return latest


def _claude_keep(entry: dict) -> bool:
    return entry.get("type") in ("user", "assistant") and not entry.get("isMeta") and not entry.get("isSidechain")


def _codex_keep(entry: dict) -> bool:
    return entry.get("type") in ("response_item", "event_msg")


def claude_last(session_id: str) -> datetime | None:
    path = claude_session_file(session_id)
    return _last_timestamp(path, _claude_keep) if path else None


def codex_last(session_id: str) -> datetime | None:
    path = codex_session_file(session_id)
    return _last_timestamp(path, _codex_keep) if path else None


READERS = {"claude": claude_last, "codex": codex_last}


def last_activity(agent: str, session_value: str) -> datetime | None:
    """Last activity from the agent's own files, or None if there is no reader or it failed."""
    reader = READERS.get(agent)
    if reader is None:
        return None
    try:
        return reader(session_value)
    except OSError:
        return None
