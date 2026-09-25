"""Optional history sources: read an agent's own session files for its last activity."""

from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime
from pathlib import Path

from .util import parse_iso

# Session ids come from agent_session values reported by herdr (or read back
# from our own records), but they end up embedded in filesystem glob patterns,
# so a malformed or malicious one must not be able to read outside the
# expected directory (e.g. "../../outside/secret") or contain a path
# separator (e.g. "a/b").
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _valid_session_id(session_id: str) -> bool:
    # fullmatch (rather than match with a trailing "$") is required here:
    # "$" matches just before a trailing "\n", so a match()-based check would
    # incorrectly accept an id like "abc\n".
    return isinstance(session_id, str) and bool(_SESSION_ID_RE.fullmatch(session_id))


def _newest(matches: list[str]) -> Path | None:
    """The match with the latest mtime, or None. Several files can share a
    session id across projects or after a rename; the newest one is the
    session that is actually still in use. A match that no longer exists
    (removed between glob() and stat()) is skipped rather than raising."""
    best, best_mtime = None, None
    for m in matches:
        path = Path(m)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if best_mtime is None or mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def claude_session_file(session_id: str) -> Path | None:
    if not _valid_session_id(session_id):
        return None
    pattern = str(claude_home() / "projects" / "*" / f"{glob.escape(session_id)}.jsonl")
    return _newest(glob.glob(pattern))


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
    if not _valid_session_id(session_id):
        return None
    pattern = str(codex_home() / "sessions" / "**" / f"rollout-*-{glob.escape(session_id)}.jsonl")
    return _newest(glob.glob(pattern, recursive=True))


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
    except Exception:
        return None
