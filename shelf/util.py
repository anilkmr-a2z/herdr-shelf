"""Small helpers: UTC timestamps, atomic JSON files, and a lock file."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc
_FRACTION = re.compile(r"\.(\d+)")
_OFFSET_NO_COLON = re.compile(r"([+-]\d{2})(\d{2})$")
_LOG = logging.getLogger("shelf")


def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp such as 2026-09-24T10:15:00.123Z.

    Returns an aware UTC datetime, or None when the value is not a timestamp.
    Naive timestamps are taken as UTC. Fractions of any length are accepted.
    A trailing numeric offset without a colon (+HHMM) is normalized to +HH:MM
    so this parses the same way on Python 3.9 and 3.12.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip().replace("Z", "+00:00")
    s = _OFFSET_NO_COLON.sub(r"\1:\2", s)
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
    except ValueError:
        _LOG.warning("%s is not valid JSON; ignoring it", path)
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
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


class LockBusy(Exception):
    """Another process holds the lock."""


class FileLock:
    """Exclusive lock using fcntl.flock on a lock file.

    wait_seconds: how long to retry before raising LockBusy (0 means try once).
    The lock is held by an open file descriptor, so if the holding process dies
    (even via SIGKILL) the kernel releases the lock automatically. The lock
    file itself is never deleted.
    """

    def __init__(self, path: Path, wait_seconds: float = 0.0):
        self.path = Path(path)
        self.wait_seconds = wait_seconds
        self._fd: int | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + self.wait_seconds
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockBusy(str(self.path))
                    time.sleep(0.05)
                    continue
                break
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *exc: Any) -> bool:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        return False
