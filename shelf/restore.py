"""Rebuild an archived tab and resume its agents."""

from __future__ import annotations

from datetime import datetime

from . import activity, agents, history


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
    """Restore one archived tab. Returns {"tab_id", "warnings"}; the entry is deleted on success."""
    record = arch.load(archive_id)
    arch.put_back_sessions(record)
    warnings = []
    for meta in record.get("panes", {}).values():
        if meta.get("agent") == "claude" and not history.claude_session_file(meta["session"]["value"]):
            warnings.append(f"{record['tab'].get('label')}: Claude conversation {meta['session']['value']} "
                            "was not found, so it may not resume")
    params = {"root": build_tree(record["layout"]["root"], record.get("panes", {}), table),
              "tab_label": record["tab"].get("label"), "focus": True}
    workspace = record.get("workspace", {})
    workspace_id = _find_workspace(client, workspace.get("label"))
    if workspace_id:
        params["workspace_id"] = workspace_id
    else:
        create = {k: v for k, v in {"label": workspace.get("label"), "cwd": workspace.get("cwd")}.items() if v}
        create["focus"] = True
        created = client.call("workspace.create", create)
        params["workspace_id"] = created["workspace"]["workspace_id"]
        params["tab_id"] = created["tab"]["tab_id"]
    result = client.call("layout.apply", params)
    keys = [activity.session_key(m["agent"], m["session"]["value"])
            for m in record.get("panes", {}).values() if m.get("agent") and m.get("session")]

    def mark(data):
        for key in keys:
            activity.mark_restored(data, key, now)
        return True

    store.update(mark)
    arch.delete(archive_id)
    return {"tab_id": (result.get("layout") or {}).get("tab_id"), "warnings": warnings}
