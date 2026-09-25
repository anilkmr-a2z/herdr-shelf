"""Small helpers: UTC timestamps, atomic JSON files, and a lock file."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc
_FRACTION = re.compile(r"\.(\d+)")


def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp such as 2026-09-24T10:15:00.123Z.

    Returns an aware UTC datetime, or None when the value is not a timestamp.
    Naive timestamps are taken as UTC. Fractions of any length are accepted.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip().replace("Z", "+00:00")
    s = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON to a temporary file in the same directory, then rename it into place."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class LockBusy(Exception):
    """Another process holds the lock."""


class FileLock:
    """Exclusive lock file created with O_EXCL.

    wait_seconds: how long to retry before raising LockBusy (0 means try once).
    stale_seconds: a lock file older than this is assumed abandoned and removed.
    """

    def __init__(self, path: Path, wait_seconds: float = 0.0, stale_seconds: float = 600.0):
        self.path = Path(path)
        self.wait_seconds = wait_seconds
        self.stale_seconds = stale_seconds

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.wait_seconds
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > self.stale_seconds:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise LockBusy(str(self.path))
                time.sleep(0.05)
                continue
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return self

    def __exit__(self, *exc: Any) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        return False
