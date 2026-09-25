"""Rebuild an archived tab and resume its agents."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from . import activity, agents, archive, history, picker
from .api import HerdrError
from .util import FileLock

log = logging.getLogger("shelf")

# How long restore() waits for sweep.lock before giving up. A module constant
# (rather than a literal at the call site) so tests can shrink it and avoid a
# slow test when they intentionally hold the lock.
RESTORE_LOCK_WAIT_SECONDS = 30.0


def build_tree(node: dict, panes_meta: dict, table: dict, argv_log: dict | None = None) -> dict:
    """The layout.apply tree: same splits, agent panes resume, shell panes get a shell.

    argv_log, when given, collects each agent pane's relaunch argv keyed by
    pane_id as a side effect, so a caller can log exactly what was used
    without calling agents.relaunch_argv a second time -- that call can itself
    log a warning (a saved command that looked like it carried a prompt), and
    a second call would log it again.
    """
    if node.get("type") == "split":
        return {
            "type": "split",
            "direction": node["direction"],
            "ratio": node["ratio"],
            "first": build_tree(node["first"], panes_meta, table, argv_log),
            "second": build_tree(node["second"], panes_meta, table, argv_log),
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
        if argv_log is not None:
            argv_log[node.get("pane_id")] = argv
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
        label = picker.record_label(record)
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

        # Before touching anything else (in particular before layout.apply,
        # or workspace.create for a recreated workspace): if this conversation
        # is already open in a live tab, restoring it here would put the same
        # conversation in two tabs at once. Refuse and keep the archive entry.
        live_panes = client.call("pane.list").get("panes", [])
        live_sessions = {p["agent_session"]["value"] for p in live_panes
                         if isinstance(p.get("agent_session"), dict) and p["agent_session"].get("value")}
        for meta in panes.values():
            value = (meta.get("session") or {}).get("value")
            if value and value in live_sessions:
                raise archive.Skip(f"conversation {value[:8]} is already open in another tab; close it first")

        argv_log = {}
        params = {"root": build_tree(record["layout"]["root"], panes, table, argv_log),
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

        tab_id = (result.get("layout") or {}).get("tab_id")
        keys = [activity.session_key(m["agent"], m["session"]["value"])
                for m in panes.values() if m.get("agent") and m.get("session")]

        def mark(data):
            for key in keys:
                activity.mark_restored(data, key, now)
            return True

        # Logged before the entry is deleted below, so if the relaunch itself
        # then fails (the agent's own process starting up, out of Shelf's
        # control), the session id it was relaunched with is still in
        # shelf.log rather than gone along with the archive entry.
        log.info("restored %s into %s", archive_id, tab_id)
        for pane_id, argv in argv_log.items():
            meta = panes.get(pane_id, {})
            session = meta.get("session") or {}
            if meta.get("agent") and session.get("value"):
                log.info("%s: %s", activity.session_key(meta["agent"], session["value"]), argv)

        try:
            store.update(mark)
        except Exception as e:
            # A bookkeeping failure must never leave a successfully restored
            # tab's archive entry behind.
            log.warning("failed to record restored activity for %s: %s", archive_id, e)
        finally:
            arch.delete(archive_id)
        return {"tab_id": tab_id, "warnings": warnings}
