"""Per-agent resume arguments, and building the command that resumes a session."""

from __future__ import annotations

import os
import shlex

# Mirrors herdr's src/agent_resume.rs: the commands herdr itself uses to resume
# agent sessions after a server restart. herdr does not expose them through its
# API, so they are copied here. Compare with herdr's current source before each
# release and add any new agents.
#
# program:          executable to look for in the pane, and to run for a plain relaunch
# resume:           resume arguments; "{id}" is replaced by the session value
# strip:            extra flags that take a value, removed from a saved launch argv
# strip_bare:       extra flags without a value, removed from a saved launch argv
# strip_subcommand: a subcommand plus its argument, removed from a saved launch argv
BUILTIN = {
    "claude": {"program": "claude", "resume": ["--resume", "{id}"], "strip": ["-r"], "strip_bare": ["--continue", "-c"]},
    "codex": {"program": "codex", "resume": ["resume", "{id}"], "strip_subcommand": "resume"},
    "copilot": {"program": "copilot", "resume": ["--resume={id}"]},
    "devin": {"program": "devin", "resume": ["--resume", "{id}"]},
    "droid": {"program": "droid", "resume": ["--resume", "{id}"]},
    "kimi": {"program": "kimi", "resume": ["--session", "{id}"]},
    "mastracode": {"program": "mastracode", "resume": ["--thread", "{id}"]},
    "pi": {"program": "pi", "resume": ["--session", "{id}"]},
    "omp": {"program": "omp", "resume": ["--resume={id}"], "strip": ["-r"]},
    "hermes": {"program": "hermes", "resume": ["--resume", "{id}"]},
    "opencode": {"program": "opencode", "resume": ["--session", "{id}"]},
    "qodercli": {"program": "qodercli", "resume": ["--resume", "{id}"]},
    "qwen": {"program": "qwen", "resume": ["--resume", "{id}"]},
    "kilo": {"program": "kilo", "resume": ["--session", "{id}"]},
    "cursor": {"program": "cursor-agent", "resume": ["--resume", "{id}"]},
    "agy": {"program": "agy", "resume": ["--conversation", "{id}"]},
    "grok": {"program": "grok", "resume": ["--resume", "{id}"]},
    "letta": {"program": "letta", "resume": ["--conversation", "{id}"]},
}


def table(overrides: dict | None = None) -> dict:
    """The built-in table with config overrides merged per agent.

    Entries without both a program and resume arguments are dropped.
    """
    merged = {name: dict(entry) for name, entry in BUILTIN.items()}
    for name, override in (overrides or {}).items():
        merged[name] = {**merged.get(name, {}), **override}
    return {name: e for name, e in merged.items() if e.get("program") and e.get("resume")}


def resume_args(agent: str, entry: dict, session_value: str) -> list[str]:
    if agent == "letta" and session_value.startswith("default:"):
        return ["--conversation", "default", "--agent", session_value[len("default:"):]]
    return [part.replace("{id}", session_value) for part in entry["resume"]]


def _own_flag(entry: dict) -> str | None:
    first = entry["resume"][0]
    if not first.startswith("-"):
        return None
    return first.split("=", 1)[0]


def strip_resume(argv: list[str], entry: dict) -> list[str]:
    """Remove any previous resume or continue arguments. argv[0] is never touched."""
    value_flags = set(entry.get("strip", []))
    own = _own_flag(entry)
    if own:
        value_flags.add(own)
    bare = set(entry.get("strip_bare", []))
    subcommand = entry.get("strip_subcommand")
    out = argv[:1]
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in value_flags or (subcommand and tok == subcommand):
            i += 2
            continue
        if tok in bare or any(tok.startswith(flag + "=") for flag in value_flags):
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def relaunch_argv(agent: str, entry: dict, session_value: str, launch_argv: list[str] | None) -> list[str]:
    args = resume_args(agent, entry, session_value)
    if entry.get("relaunch") == "plain" or not launch_argv:
        return [entry["program"], *args]
    return [*strip_resume(list(launch_argv), entry), *args]


def matches_program(entry: dict, argv0: str) -> bool:
    return os.path.basename(argv0) == entry["program"]


def shell_command(argv: list[str]) -> list[str]:
    """Run argv, then leave an interactive shell in the pane when it exits."""
    return ["sh", "-c", shlex.join(argv) + '; exec "${SHELL:-sh}"']
