"""Self-healing merge of pre-0.3.0 root-level state into sessions/default/.

Before per-session state, activity.json, archive/, last_sweep and
installed_at lived directly under the plugin's state root. This module
merges anything still there into sessions/default/, without ever
overwriting or losing anything.

The presence of sessions/ is never treated as "already migrated": a
downgrade to a pre-0.3.0 build can write new root-level state after an
earlier merge (the "rollback" case), and a merge interrupted partway
through (for example the process was killed) must finish on a later run.
merge_into_default_session is therefore called on every plugin invocation,
and is cheap (a handful of existence checks, no lock) when there is nothing
to do.
"""

from __future__ import annotations

import filecmp
import logging
import os
import shutil
from pathlib import Path

from .util import FileLock, LockBusy, atomic_write_json, parse_iso, read_json

log = logging.getLogger("shelf")

# How long merge_into_default_session waits for each lock before giving up
# (logging a warning and leaving everything at the root for the next
# invocation to retry) rather than blocking indefinitely.
LOCK_WAIT_SECONDS = 10.0


def root_legacy_present(root: Path) -> bool:
    """Cheap, lock-free check: is there any pre-0.3.0 root-level state left
    to merge? Used to skip locking and directory creation entirely for a
    fresh install, or once everything has already been migrated.
    """
    if (root / "activity.json").exists() or (root / "last_sweep").exists() or (root / "installed_at").exists():
        return True
    archive_dir = root / "archive"
    try:
        return archive_dir.is_dir() and any(archive_dir.iterdir())
    except OSError:
        return False


def merge_into_default_session(root: Path) -> None:
    """Merge root-level legacy state into sessions/default/, if any exists.

    Takes migrate.lock, plus the root-level sweep.lock and activity.lock --
    the ones a still-running pre-0.3.0 process uses -- so such a process
    (for example after a rollback) is excluded for the duration of the
    merge, each waiting up to LOCK_WAIT_SECONDS. Never raises: a lock that
    cannot be acquired, or a failure partway through, is logged and left
    for the next invocation to retry, so the command that triggered this
    call always gets to run regardless.
    """
    if not root_legacy_present(root):
        return
    try:
        with FileLock(root / "migrate.lock", wait_seconds=LOCK_WAIT_SECONDS), \
                FileLock(root / "sweep.lock", wait_seconds=LOCK_WAIT_SECONDS), \
                FileLock(root / "activity.lock", wait_seconds=LOCK_WAIT_SECONDS):
            _merge(root)
    except LockBusy:
        log.warning("could not migrate legacy state into sessions/default: "
                    "a lock is held elsewhere; will retry")
    except OSError:
        log.exception("failed to migrate legacy state into sessions/default; will retry")


def _merge(root: Path) -> None:
    default_dir = root / "sessions" / "default"
    default_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for name, merge_fn in (
        ("archive", lambda: _merge_archive(root, default_dir)),
        ("activity.json", lambda: _merge_activity_json(root, default_dir)),
        ("last_sweep", lambda: _merge_marker(root, default_dir, "last_sweep", keep="destination")),
        ("installed_at", lambda: _merge_marker(root, default_dir, "installed_at", keep="earlier")),
    ):
        try:
            if merge_fn():
                moved.append(name)
        except OSError:
            log.exception("failed to migrate %s into sessions/default", name)
    if moved:
        log.info("migrated legacy state into sessions/default: %s", ", ".join(moved))
    if root_legacy_present(root):
        log.error("some legacy state could not be migrated into sessions/default "
                  "(see warnings above); will retry on the next command")


def _archive_entries_identical(a: Path, b: Path) -> bool:
    """Whether two archive folders (record.json plus any copied session
    files under sessions/) are byte-identical, file for file."""
    files_a = sorted(p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(b).as_posix() for p in b.rglob("*") if p.is_file())
    if files_a != files_b:
        return False
    return all(filecmp.cmp(str(a / rel), str(b / rel), shallow=False) for rel in files_a)


def _resolve_archive_collision(root: Path, entry: Path, dest: Path) -> bool:
    """entry (root/archive/<id>) collides with dest (sessions/default/
    archive/<id>), which is never overwritten. If the two are identical,
    the root copy is simply redundant and is deleted. Otherwise it is a
    genuine conflict: moved to root/archive.conflict/<id> (outside
    archive/, so root_legacy_present no longer sees it, and this does not
    warn again on a later invocation) and logged once. Returns whether the
    collision was actually resolved (false in the rare case where
    archive.conflict/<id> itself already exists too, in which case the
    root copy is left in place, unresolved, exactly as before this
    function existed).
    """
    if _archive_entries_identical(entry, dest):
        shutil.rmtree(entry)
        return True
    conflict_dir = root / "archive.conflict"
    conflict_dest = conflict_dir / entry.name
    if conflict_dest.exists():
        log.warning("archive %s already has a conflicting copy at archive.conflict/%s; "
                    "leaving the root copy in place", entry.name, entry.name)
        return False
    conflict_dir.mkdir(parents=True, exist_ok=True)
    os.replace(str(entry), str(conflict_dest))
    log.warning("archive %s exists at both the root and in sessions/default, and differs; "
                "moved the root copy to archive.conflict/%s", entry.name, entry.name)
    return True


def _merge_archive(root: Path, default_dir: Path) -> bool:
    src_root = root / "archive"
    if not src_root.is_dir():
        return False
    dest_root = default_dir / "archive"
    moved_any = False
    for entry in sorted(src_root.iterdir()):
        dest = dest_root / entry.name
        if dest.exists():
            try:
                if _resolve_archive_collision(root, entry, dest):
                    moved_any = True
            except OSError:
                log.exception("failed to resolve archive %s collision during migration", entry.name)
            continue
        try:
            dest_root.mkdir(parents=True, exist_ok=True)
            os.replace(str(entry), str(dest))
            moved_any = True
        except OSError:
            log.exception("failed to migrate archive %s into sessions/default", entry.name)
    try:
        next(src_root.iterdir())
    except StopIteration:
        src_root.rmdir()
    except OSError:
        pass
    return moved_any


def _merge_activity_json(root: Path, default_dir: Path) -> bool:
    """Merge root activity.json into sessions/default/activity.json.

    Takes sessions/default/activity.lock -- the same lock
    activity.ActivityStore uses -- for the read-merge-write of the
    destination, so a concurrent ActivityStore.update() for this session
    (for example from a track hook running at the same time) is properly
    serialized with this merge rather than racing it: whichever actually
    runs first, the other sees its result and neither write is lost.
    """
    src = root / "activity.json"
    if not src.exists():
        return False
    dest = default_dir / "activity.json"
    default_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(default_dir / "activity.lock", wait_seconds=LOCK_WAIT_SECONDS):
        if not dest.exists():
            os.replace(str(src), str(dest))
            return True
        merged = _merge_activity_data(read_json(dest, {}), read_json(src, {}))
        atomic_write_json(dest, merged)
    src.unlink()
    return True


def _later_iso_value(a, b):
    """Whichever of a and b (ISO timestamp strings, or None) is later.

    Falls back to whichever side has a value at all when neither parses,
    and to None when neither does.
    """
    pa, pb = parse_iso(a), parse_iso(b)
    if pa is None and pb is None:
        return a if a is not None else b
    if pa is None:
        return b
    if pb is None:
        return a
    return a if pa >= pb else b


def _merge_session_record(dest_rec: dict, src_rec: dict) -> dict:
    """Merge one "<agent>:<session>" activity record field by field.

    last_active, restored_at and agent_started_at each independently keep
    whichever side has the later value; every other field keeps the
    destination's value, falling back to the source's when the destination
    doesn't have it at all.
    """
    merged = dict(dest_rec)
    for field in ("last_active", "restored_at", "agent_started_at"):
        value = _later_iso_value(dest_rec.get(field), src_rec.get(field))
        if value is None:
            merged.pop(field, None)
        else:
            merged[field] = value
    for key, value in src_rec.items():
        if key not in merged:
            merged[key] = value
    return merged


def _merge_terminal_entry(dest_entry, src_entry):
    if not isinstance(dest_entry, dict):
        return src_entry
    if not isinstance(src_entry, dict):
        return dest_entry
    started = _later_iso_value(dest_entry.get("agent_started_at"), src_entry.get("agent_started_at"))
    return {"agent_started_at": started} if started else dict(dest_entry)


def _merge_activity_data(dest_data, src_data) -> dict:
    dest_data = dest_data if isinstance(dest_data, dict) else {}
    src_data = src_data if isinstance(src_data, dict) else {}
    merged = dict(dest_data)
    for key, rec in src_data.items():
        if key == "terminals" or not isinstance(rec, dict):
            continue
        if isinstance(merged.get(key), dict):
            merged[key] = _merge_session_record(merged[key], rec)
        else:
            merged[key] = rec
    dest_terminals = dest_data.get("terminals")
    src_terminals = src_data.get("terminals")
    dest_terminals = dest_terminals if isinstance(dest_terminals, dict) else {}
    src_terminals = src_terminals if isinstance(src_terminals, dict) else {}
    terminals = dict(dest_terminals)
    for term_id, entry in src_terminals.items():
        terminals[term_id] = _merge_terminal_entry(terminals.get(term_id), entry)
    if terminals:
        merged["terminals"] = terminals
    return merged


def _merge_marker(root: Path, default_dir: Path, name: str, keep: str) -> bool:
    """Merge a single-value marker file (last_sweep, installed_at).

    keep="destination": the destination wins whenever it already exists.
    keep="earlier": when both exist, the earlier ISO timestamp wins.
    Either way, the root copy is removed once merged.
    """
    src = root / name
    if not src.exists():
        return False
    dest = default_dir / name
    if not dest.exists():
        os.replace(str(src), str(dest))
        return True
    if keep == "earlier":
        src_ts = parse_iso(src.read_text().strip())
        dest_ts = parse_iso(dest.read_text().strip())
        if src_ts is not None and (dest_ts is None or src_ts < dest_ts):
            os.replace(str(src), str(dest))
            return True
    src.unlink()
    return True
