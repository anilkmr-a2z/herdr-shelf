"""The popup that lists archived tabs and restores one."""

from __future__ import annotations

from datetime import datetime

from .util import parse_iso


def _days_idle(record: dict, now: datetime):
    stamps = [parse_iso(m.get("last_activity")) for m in record.get("panes", {}).values()]
    stamps = [s for s in stamps if s is not None]
    return (now - max(stamps)).days if stamps else None


def render(records: list, now: datetime) -> list:
    if not records:
        return ["No archived tabs."]
    lines = []
    for number, rec in enumerate(records, 1):
        tab = rec.get("tab", {}).get("label") or "(unnamed)"
        workspace = rec.get("workspace", {}).get("label") or ""
        agent_names = ",".join(sorted({m["agent"] for m in rec.get("panes", {}).values() if m.get("agent")}))
        days = _days_idle(rec, now)
        idle = "" if days is None else f"{days}d idle"
        lines.append(f"{number:>3}  {tab:<28} {workspace:<20} {agent_names:<12} {idle}".rstrip())
    return lines


def parse_choice(text: str, count: int):
    t = text.strip().lower()
    if t in ("", "q", "quit"):
        return ("quit", None)
    if t.startswith("d"):
        n = t[1:].strip()
        if n.isdigit() and 1 <= int(n) <= count:
            return ("delete", int(n) - 1)
        return ("invalid", None)
    if t.isdigit() and 1 <= int(t) <= count:
        return ("restore", int(t) - 1)
    return ("invalid", None)


def run(arch, do_restore, now_fn, input_fn=input, print_fn=print) -> None:
    while True:
        records = arch.list()
        print_fn("\n".join(render(records, now_fn())))
        if not records:
            input_fn("Press Enter to close. ")
            return
        choice, index = parse_choice(input_fn("Number to restore, d<number> to delete, q to quit: "), len(records))
        if choice == "quit":
            return
        if choice == "invalid":
            print_fn("Not a valid choice.")
            continue
        record = records[index]
        if choice == "delete":
            arch.delete(record["id"])
            continue
        try:
            result = do_restore(record["id"])
        except Exception as e:  # keep the entry and the popup; show why
            print_fn(f"Restore failed: {e}")
            input_fn("Press Enter to continue. ")
            continue
        for warning in result.get("warnings", []):
            print_fn(warning)
        if result.get("warnings"):
            input_fn("Press Enter to close. ")
        return
