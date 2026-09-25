"""Rebuild an archived tab and resume its agents."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from . import activity, agents, history
from .api import HerdrError
from .util import FileLock

log = logging.getLogger("shelf")

# How long restore() waits for sweep.lock before giving up. A module constant
# (rather than a literal at the call site) so tests can shrink it and avoid a
# slow test when they intentionally hold the lock.
RESTORE_LOCK_WAIT_SECONDS = 30.0


def build_tree(node: dict, panes_meta: dict, table: dict) -> dict:
    """The layout.apply tree: same splits, agent panes resume, shell panes get a shell."""
    if node.get("type") == "split":
        return {
            "type": "split",
            "direction": node["direction"],
            "ratio": node["ratio"],
            "first": build_tree(node["first"], panes_meta, table),
            "second": build_tree(node["second"], panes_meta, table),
        }
    meta = panes_meta.get(node.get("pane_id"), {})
    out = {"type": "pane"}
    cwd = node.get("cwd") or meta.get("cwd")
    if cwd:
        out["cwd"] = cwd
    if node.get("label"):
        out["label"] = node["label"]
    agent = meta.get("agent")
    if agent in table and meta.get("session", {}).get("value"):
        argv = agents.relaunch_argv(agent, table[agent], meta["session"]["value"], meta.get("launch_argv"))
        out["command"] = agents.shell_command(argv)
    return out


def _find_workspace(client, label):
    if not label:
        return None
    for ws in client.call("workspace.list").get("workspaces", []):
        if ws.get("label") == label:
            return ws["workspace_id"]
    return None


def restore(client, arch, store, archive_id: str, table: dict, now: datetime) -> dict:
    """Restore one archived tab. Returns {"tab_id", "warnings"}; the entry is deleted on success.

    Runs under the same sweep.lock a sweep uses, so a restore and a sweep (or
    a concurrent restore of the same entry) never race on the archive.
    """
    state = Path(arch.root).parent
    with FileLock(state / "sweep.lock", wait_seconds=RESTORE_LOCK_WAIT_SECONDS):
        record = arch.load(archive_id)  # KeyError if another process already restored it
        arch.put_back_sessions(record)
        panes = record.get("panes", {})
        # The tab's own label is usually most specific; fall back to the
        # workspace label, then a generic word, rather than literally "None".
        label = record["tab"].get("label") or record.get("workspace", {}).get("label") or "tab"
        warnings = []
        for meta in panes.values():
            if meta.get("agent") == "claude" and not history.claude_session_file(meta["session"]["value"]):
                warnings.append(f"{label}: Claude conversation {meta['session']['value']} "
                                "was not found, so it may not resume")
        missing_cwds = sorted({meta["cwd"] for meta in panes.values()
                                if meta.get("cwd") and not os.path.isdir(meta["cwd"])})
        for cwd in missing_cwds:
            warnings.append(f"{label}: {cwd} no longer exists; "
                            "the pane opens in herdr's fallback directory")

        params = {"root": build_tree(record["layout"]["root"], panes, table),
                  "tab_label": record["tab"].get("label"), "focus": True}
        workspace = record.get("workspace", {})
        workspace_id = _find_workspace(client, workspace.get("label"))
        created_workspace_id = None
        if workspace_id:
            params["workspace_id"] = workspace_id
        else:
            # layout.apply rejects a request that carries both tab_id and
            # workspace_id, so a newly created workspace is targeted by its
            # first tab's id alone.
            create = {k: v for k, v in {"label": workspace.get("label"), "cwd": workspace.get("cwd")}.items() if v}
            create["focus"] = True
            created = client.call("workspace.create", create)
            created_workspace_id = created["workspace"]["workspace_id"]
            params["tab_id"] = created["tab"]["tab_id"]

        try:
            result = client.call("layout.apply", params)
        except HerdrError:
            if created_workspace_id:
                try:
                    client.call("workspace.close", {"workspace_id": created_workspace_id})
                except HerdrError as close_err:
                    log.warning("failed to clean up workspace %s after a failed restore: %s",
                                created_workspace_id, close_err)
            raise

        keys = [activity.session_key(m["agent"], m["session"]["value"])
                for m in panes.values() if m.get("agent") and m.get("session")]

        def mark(data):
            for key in keys:
                activity.mark_restored(data, key, now)
            return True

        try:
            store.update(mark)
        except Exception as e:
            # A bookkeeping failure must never leave a successfully restored
            # tab's archive entry behind.
            log.warning("failed to record restored activity for %s: %s", archive_id, e)
        finally:
            arch.delete(archive_id)
        return {"tab_id": (result.get("layout") or {}).get("tab_id"), "warnings": warnings}
