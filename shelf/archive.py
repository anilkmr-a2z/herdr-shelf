"""Capture a tab into an archive record, and store records on disk."""

from __future__ import annotations

import logging
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from . import agents, history
from .api import HerdrError
from .util import atomic_write_json, iso, read_json

log = logging.getLogger("shelf")


class Skip(Exception):
    """The tab cannot be archived right now. Nothing was changed."""


class Archive:
    """Records live in <state>/archive/<id>/record.json, session copies under sessions/."""

    def __init__(self, state_dir):
        self.root = Path(state_dir) / "archive"

    def _dir(self, archive_id: str) -> Path:
        return self.root / archive_id

    def save(self, record: dict, session_files: list) -> None:
        """session_files: (source path, path relative to the Claude home) pairs to copy."""
        folder = self._dir(record["id"])
        for src, rel in session_files:
            dest = folder / "sessions" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if Path(src).is_dir():
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)
        atomic_write_json(folder / "record.json", record)

    def list(self) -> list:
        records = []
        if self.root.is_dir():
            for path in self.root.glob("*/record.json"):
                rec = read_json(path, None)
                if isinstance(rec, dict) and rec.get("id"):
                    records.append(rec)
        return sorted(records, key=lambda r: r.get("archived_at", ""), reverse=True)

    def load(self, archive_id: str) -> dict:
        rec = read_json(self._dir(archive_id) / "record.json", None)
        if not isinstance(rec, dict):
            raise KeyError(archive_id)
        return rec

    def delete(self, archive_id: str) -> None:
        shutil.rmtree(self._dir(archive_id), ignore_errors=True)

    def put_back_sessions(self, record: dict) -> list:
        """Copy archived Claude session files back where Claude deleted them."""
        restored = []
        base = history.claude_home()
        for rel in record.get("session_copies", []):
            target = base / rel
            src = self._dir(record["id"]) / "sessions" / rel
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

    Wrappers often re-exec the agent with extra injected arguments, and herdr
    orders processes by pid, which can wrap. Among the matching processes the
    one with the fewest arguments is the outermost, user-typed command.
    """
    info = client.call("pane.process_info", {"pane_id": pane_id}).get("process_info") or {}
    matches = []
    for proc in info.get("foreground_processes", []):
        argv = [a for a in (proc.get("argv") or []) if isinstance(a, str)]
        if argv and (proc.get("name") == entry["program"] or agents.matches_program(entry, argv[0])):
            matches.append(argv)
    return min(matches, key=len) if matches else None


def _workspace_label(client, workspace_id: str):
    for ws in client.call("workspace.list").get("workspaces", []):
        if ws.get("workspace_id") == workspace_id:
            return ws.get("label")
    return None


def capture(client, tab: dict, panes: list, table: dict, activity_of, keep_transcripts: bool, now: datetime):
    """Build the archive record for a tab. Returns (record, session_files)."""
    layout = client.call("layout.export", {"tab_id": tab["tab_id"]}).get("layout")
    if not layout or "root" not in layout:
        raise Skip("layout.export returned no layout")
    workspace_label = _workspace_label(client, tab["workspace_id"])
    pane_meta, session_files, copies = {}, [], []
    for pane in panes:
        meta = {"cwd": pane.get("cwd")}
        session = pane.get("agent_session")
        if pane.get("agent") and session and session.get("agent") in table:
            agent = session["agent"]
            last = activity_of(agent, session["value"])
            launch_argv = _launch_argv(client, pane["pane_id"], table[agent])
            if launch_argv is None:
                log.warning("%s: no %s process found; it will restore with a plain resume",
                            pane["pane_id"], table[agent]["program"])
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
    record = {
        "version": 1,
        "id": new_id(now),
        "archived_at": iso(now),
        "workspace": {"label": workspace_label, "cwd": first_cwd},
        "tab": {"label": tab.get("label")},
        "layout": {"root": layout["root"], "focused_pane_id": layout.get("focused_pane_id"),
                   "zoomed": bool(layout.get("zoomed"))},
        "panes": pane_meta,
        "session_copies": copies,
    }
    return record, session_files


def archive_tab(client, arch: Archive, tab: dict, panes: list, table: dict, activity_of,
                keep_transcripts: bool, now: datetime) -> str:
    """Write the record, then close the tab. Returns the archive id."""
    record, session_files = capture(client, tab, panes, table, activity_of, keep_transcripts, now)
    arch.save(record, session_files)
    try:
        client.call("tab.close", {"tab_id": tab["tab_id"]})
    except HerdrError as e:
        # Only a definite refusal means the tab is still open. If the outcome is
        # unknown (the reply was lost), keep the record: it may be the only copy.
        if e.definite:
            arch.delete(record["id"])
        if e.code == "confirmation_required":
            raise Skip("closing it would close a worktree group") from e
        raise
    return record["id"]
