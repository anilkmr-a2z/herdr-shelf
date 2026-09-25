"""The popup that lists archived tabs and restores one."""

from __future__ import annotations

import shutil
import signal
import sys
from datetime import datetime

from .util import parse_iso

ESC = "\x1b"
CLEAR_SCREEN = "\033[H\033[2J"


def _days_idle(record: dict, now: datetime):
    stamps = [parse_iso(m.get("last_activity")) for m in record.get("panes", {}).values()]
    stamps = [s for s in stamps if s is not None]
    return (now - max(stamps)).days if stamps else None


def _label(record: dict) -> str:
    return record.get("tab", {}).get("label") or record.get("workspace", {}).get("label") or "tab"


def _visible_rows(height=None) -> int:
    """How many entries fit the popup, leaving room for the header and prompt."""
    rows = height if height is not None else shutil.get_terminal_size((80, 20)).lines
    return max(rows - 3, 1)


def render(records: list, now: datetime, width=None, height=None) -> list:
    if not records:
        return ["No archived tabs."]
    cols = width if width is not None else shutil.get_terminal_size((80, 20)).columns
    limit = max(cols - 1, 20)
    max_entries = _visible_rows(height)
    shown = records[:max_entries]
    # Budget the line: "NNN  " + agent (12) + " " + an idle reserve + separators,
    # then split what is left between the tab and workspace columns.
    fixed = 3 + 2 + 12 + 1 + 10 + 2
    room = max(limit - fixed, 10)
    tab_w = max(int(room * 0.6), 8)
    ws_w = max(room - tab_w, 6)
    lines = []
    for number, rec in enumerate(shown, 1):
        tab = (rec.get("tab", {}).get("label") or "(unnamed)")[:tab_w]
        workspace = (rec.get("workspace", {}).get("label") or "")[:ws_w]
        agent_names = ",".join(sorted({m["agent"] for m in rec.get("panes", {}).values() if m.get("agent")}))[:12]
        days = _days_idle(rec, now)
        idle = "" if days is None else f"{days}d idle"
        line = f"{number:>3}  {tab:<{tab_w}} {workspace:<{ws_w}} {agent_names:<12} {idle}".rstrip()
        lines.append(line[:limit])
    remaining = len(records) - len(shown)
    if remaining > 0:
        lines.append(f"{remaining} more; run `python3 -m shelf list` for all")
    return lines


def parse_choice(text: str, count: int):
    if text == ESC:
        return ("quit", None)
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


def _ignore_sigint():
    """Best-effort: signal handling can only be changed from the main thread."""
    try:
        return signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        return None


def _restore_sigint(previous) -> None:
    if previous is None:
        return
    try:
        signal.signal(signal.SIGINT, previous)
    except (ValueError, OSError):
        pass


def run(arch, do_restore, now_fn, input_fn=input, print_fn=print, notify=lambda title, body: None) -> None:
    pending_message = None
    while True:
        records = arch.list()
        if sys.stdout.isatty():
            # Written directly, bypassing print_fn: a real clear must not add
            # the trailing newline print() would, and pending_message below
            # is what needs to reach print_fn just above the next prompt.
            sys.stdout.write(CLEAR_SCREEN)
        print_fn("\n".join(render(records, now_fn())))
        if pending_message:
            print_fn(pending_message)
            pending_message = None
        if not records:
            input_fn("Press Enter to close. ")
            return
        shown = min(len(records), _visible_rows())
        choice, index = parse_choice(input_fn("Number to restore, d<number> to delete, q to quit: "), shown)
        if choice == "quit":
            return
        if choice == "invalid":
            # Held until the next render (above) instead of printed here, so
            # a screen clear on the next loop does not erase it unseen.
            pending_message = "Not a valid choice."
            continue
        record = records[index]
        if choice == "delete":
            answer = input_fn(f'Delete "{_label(record)}"? This cannot be undone. [y/N] ')
            if answer.strip().lower() in ("y", "yes"):
                arch.delete(record["id"])
            continue
        print_fn("Restoring...")
        previous = _ignore_sigint()
        try:
            result = do_restore(record["id"])
        except Exception as e:  # keep the entry and the popup; show why
            _restore_sigint(previous)
            print_fn(f"Restore failed: {e}")
            input_fn("Press Enter to continue. ")
            continue
        _restore_sigint(previous)
        warnings = result.get("warnings") or []
        if warnings:
            notify("shelf", "; ".join(warnings))
        return
