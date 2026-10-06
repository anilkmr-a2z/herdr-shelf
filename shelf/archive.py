"""Capture a tab into an archive record, and store records on disk."""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from . import agents, history
from .api import HerdrError
from .util import atomic_write_json, iso, read_json

log = logging.getLogger("shelf")

# YYYYMMDDTHHMMSSZ-<6 hex chars>, matching new_id() below. Archive ids come
# from disk (folder names) and from callers who read a record.json we wrote
# ourselves, but a corrupted or handcrafted id must never be used to build a
# filesystem path outside the archive root (e.g. "..", or an absolute path).
_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}")


class Skip(Exception):
    """The tab cannot be archived right now. Nothing was changed."""


# herdr's own layout.apply limits (src/app/api/layouts.rs MAX_LAYOUT_PANES,
# MAX_LAYOUT_DEPTH). A layout past either limit could never be restored, so
# capture() refuses to archive it rather than write a record that layout.apply
# would reject on restore.
MAX_LAYOUT_PANES = 24
MAX_LAYOUT_DEPTH = 16


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


class Archive:
    """Records live in <state>/archive/<id>/record.json, session copies under sessions/."""

    def __init__(self, state_dir):
        self.root = Path(state_dir) / "archive"

    def _dir(self, archive_id: str) -> Path:
        if not isinstance(archive_id, str) or not _ID_RE.fullmatch(archive_id):
            raise KeyError(archive_id)
        return self.root / archive_id

    def save(self, record: dict, session_files: list) -> None:
        """session_files: (source path, path relative to the Claude home) pairs to copy.

        If anything fails partway through, the partially written folder is
        removed rather than left around as a corrupt archive entry.
        """
        folder = self._dir(record["id"])
        try:
            for src, rel in session_files:
                dest = folder / "sessions" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if Path(src).is_dir():
                    shutil.copytree(src, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dest)
            atomic_write_json(folder / "record.json", record)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        _fsync_dir(self.root)

    def list(self) -> list:
        records = []
        if self.root.is_dir():
            for path in self.root.glob("*/record.json"):
                rec = read_json(path, None)
                if isinstance(rec, dict) and rec.get("id") == path.parent.name:
                    records.append(rec)
        return sorted(records, key=lambda r: r.get("archived_at", ""), reverse=True)

    def load(self, archive_id: str) -> dict:
        rec = read_json(self._dir(archive_id) / "record.json", None)
        if not isinstance(rec, dict):
            raise KeyError(archive_id)
        return rec

    def delete(self, archive_id: str) -> None:
        folder = self._dir(archive_id)
        try:
            (folder / "record.json").unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(folder, ignore_errors=True)

    def put_back_sessions(self, record: dict) -> list:
        """Copy archived Claude session files back where Claude deleted them."""
        restored = []
        base = history.claude_home()
        folder = self._dir(record["id"])
        for rel in record.get("session_copies", []):
            if not isinstance(rel, str) or not rel or os.path.isabs(rel) or ".." in Path(rel).parts:
                continue
            target = base / rel
            src = folder / "sessions" / rel
            if target.exists() or not src.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, target)
            else:
                shutil.copy2(src, target)
            restored.append(rel)
        return restored


def new_id(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def _launch_argv(client, pane_id: str, entry: dict):
    """The agent's command line as the user typed it.

    Wrappers often re-exec the agent with extra injected arguments, so a
    wrapper's argv is a superset of the flags the user actually typed. Among
    the matching processes, the outermost one is whichever process's
    argv[1:] (as a set) is a subset of every other match's argv[1:]; when
    several qualify, the first in herdr's own process order wins. When no
    candidate qualifies -- for example an unrelated "claude mcp serve" child
    that also matches the program name, but is not a wrapper around the
    interactive process -- there is no reliable outermost command.
    """
    info = client.call("pane.process_info", {"pane_id": pane_id}).get("process_info") or {}
    matches = []
    for proc in info.get("foreground_processes", []):
        argv = [a for a in (proc.get("argv") or []) if isinstance(a, str)]
        if argv and (proc.get("name") == entry["program"] or agents.matches_program(entry, argv[0])):
            matches.append(argv)
    if not matches:
        log.warning("%s: no %s process found; it will restore with a plain resume", pane_id, entry["program"])
        return None
    tails = [set(argv[1:]) for argv in matches]
    for argv, tail in zip(matches, tails):
        if all(tail <= other for other in tails):
            return argv
    log.warning("%s: %d %s processes found but none looks like the outermost one; "
                "it will restore with a plain resume", pane_id, len(matches), entry["program"])
    return None


def _workspace_label(client, workspace_id: str):
    for ws in client.call("workspace.list").get("workspaces", []):
        if ws.get("workspace_id") == workspace_id:
            return ws.get("label")
    return None


def _pane_ids(node: dict) -> set:
    if node.get("type") == "split":
        return _pane_ids(node["first"]) | _pane_ids(node["second"])
    pid = node.get("pane_id")
    return {pid} if pid else set()


def _layout_stats(node: dict, depth: int = 1) -> tuple:
    """(pane count, max depth), counted the same way herdr's layout.apply does
    (root at depth 1), so a layout that would fail to restore is caught here
    instead of producing an archive that can never come back."""
    if node.get("type") == "split":
        panes1, depth1 = _layout_stats(node["first"], depth + 1)
        panes2, depth2 = _layout_stats(node["second"], depth + 1)
        return panes1 + panes2, max(depth1, depth2)
    return 1, depth


def pane_terminals(panes: list) -> frozenset:
    return frozenset(p["terminal_id"] for p in panes if p.get("terminal_id"))


def _tab_terminals(client, tab_id: str) -> frozenset:
    panes = client.call("pane.list").get("panes", [])
    return frozenset(p["terminal_id"] for p in panes if p.get("tab_id") == tab_id and p.get("terminal_id"))


def _tab_with_terminals_exists(client, terminals: frozenset) -> bool:
    if not terminals:
        return False
    by_tab = {}
    for p in client.call("pane.list").get("panes", []):
        if p.get("terminal_id"):
            by_tab.setdefault(p.get("tab_id"), set()).add(p["terminal_id"])
    return terminals in (frozenset(v) for v in by_tab.values())


def capture(client, tab: dict, panes: list, table: dict, activity_of, keep_transcripts: bool, now: datetime,
           herdr_session: str = "default"):
    """Build the archive record for a tab. Returns (record, session_files).

    herdr_session is informational only: the herdr session this tab was
    archived from, recorded on the record for a human reading it later.
    """
    layout = client.call("layout.export", {"tab_id": tab["tab_id"]}).get("layout")
    if not layout or "root" not in layout:
        raise Skip("layout.export returned no layout")
    if _pane_ids(layout["root"]) != {p["pane_id"] for p in panes if p.get("pane_id")}:
        # herdr 0.9.0 tab/pane ids are positional: a close elsewhere between
        # our gather() and this call can make tab_id now mean a different tab.
        raise Skip("layout does not match the tab's panes")
    pane_count, max_depth = _layout_stats(layout["root"])
    if pane_count > MAX_LAYOUT_PANES:
        raise Skip(f"layout has {pane_count} panes; herdr's limit is {MAX_LAYOUT_PANES}")
    if max_depth > MAX_LAYOUT_DEPTH:
        raise Skip(f"layout depth is {max_depth}; herdr's limit is {MAX_LAYOUT_DEPTH}")
    workspace_label = _workspace_label(client, tab["workspace_id"])
    pane_meta, session_files, copies = {}, [], []
    for pane in panes:
        meta = {"cwd": pane.get("cwd")}
        session = pane.get("agent_session")
        # A session with no usable value is recorded as a plain shell, which
        # is what sweep.assess() tells the user will happen, rather than as an
        # agent with an empty session (or a KeyError on a missing "value").
        if pane.get("agent") and isinstance(session, dict) and session.get("value") and session.get("agent") in table:
            agent = session["agent"]
            last = activity_of(agent, session["value"], pane.get("terminal_id"))
            launch_argv = _launch_argv(client, pane["pane_id"], table[agent])
            meta.update({
                "agent": agent,
                "session": {"kind": session.get("kind"), "value": session["value"], "source": session.get("source")},
                "launch_argv": launch_argv,
                "last_activity": iso(last) if last else None,
            })
            if keep_transcripts and agent == "claude":
                for src in history.claude_session_paths(session["value"]):
                    rel = src.relative_to(history.claude_home()).as_posix()
                    session_files.append((src, rel))
                    copies.append(rel)
        pane_meta[pane["pane_id"]] = meta
    first_cwd = next((m["cwd"] for m in pane_meta.values() if m.get("cwd")), None)
    label = tab.get("label")
    if isinstance(label, str) and label.isdigit():
        label = None  # herdr's default label is just the tab number, not a custom name
    record = {
        "version": 1,
        "id": new_id(now),
        "archived_at": iso(now),
        "workspace": {"label": workspace_label, "cwd": first_cwd},
        "tab": {"label": label},
        "layout": {"root": layout["root"], "focused_pane_id": layout.get("focused_pane_id"),
                   "zoomed": bool(layout.get("zoomed"))},
        "panes": pane_meta,
        "session_copies": copies,
        "herdr_session": herdr_session,
    }
    return record, session_files


def archive_tab(client, arch: Archive, tab: dict, panes: list, table: dict, activity_of,
                keep_transcripts: bool, now: datetime, herdr_session: str = "default") -> str:
    """Write the record, verify the tab is unchanged, then close it. Returns the archive id."""
    record, session_files = capture(client, tab, panes, table, activity_of, keep_transcripts, now, herdr_session)
    arch.save(record, session_files)
    expected_terminals = pane_terminals(panes)
    try:
        terminals_now = _tab_terminals(client, tab["tab_id"])
    except BaseException:
        # Whatever went wrong, the record must not be left behind as an
        # orphan: the tab was never closed, so this record would be a
        # duplicate of a tab that is still open.
        arch.delete(record["id"])
        raise
    if terminals_now != expected_terminals:
        # Another tab closing between our gather() and here can shift
        # herdr's positional ids onto a different tab; closing tab_id now
        # would close the wrong thing, so back out instead.
        arch.delete(record["id"])
        raise Skip("tab changed before it could be closed")
    try:
        client.call("tab.close", {"tab_id": tab["tab_id"]})
    except HerdrError as e:
        # A definite refusal means the tab is untouched: delete the record.
        # Otherwise the outcome is unknown (the reply was lost); ask
        # pane.list whether the tab is still there before deciding, rather
        # than guessing.
        if e.definite:
            arch.delete(record["id"])
        else:
            try:
                if _tab_with_terminals_exists(client, expected_terminals):
                    arch.delete(record["id"])
            except HerdrError:
                pass  # can't confirm the outcome; keep the record rather than risk losing the only copy
        if e.code == "confirmation_required":
            raise Skip("closing it would close a worktree group") from e
        raise
    return record["id"]
