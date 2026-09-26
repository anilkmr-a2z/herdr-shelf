"""Which herdr session a socket path belongs to.

Each herdr session has its own socket: the default session's is
"<herdr config dir>/herdr.sock", and a named session <name>'s is
"<herdr config dir>/sessions/<name>/herdr.sock". Plugin commands get
HERDR_SOCKET_PATH for the session they run in, so the session name is
recovered from that path alone.
"""

from __future__ import annotations

import os
import re

# herdr's own session name rule: letters, digits, '.', '_' or '-', 1-64
# characters. "." and ".." are excluded separately below, since the regex
# alone would accept them.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_RESERVED_NAMES = (".", "..")


def _valid_session_name(name) -> bool:
    return isinstance(name, str) and bool(_NAME_RE.match(name)) and name not in _RESERVED_NAMES


def herdr_session_name(socket_path):
    """The herdr session name for a HERDR_SOCKET_PATH value.

    The path is normalized (os.path.normpath) first, so ".." components and
    doubled separators resolve the way a real filesystem path would, before
    any name is extracted from it.

    Returns the <name> in a normalized path ending
    "sessions/<name>/herdr.sock" (validated against herdr's session name
    rule), or "default" for anything that does not name a session at all --
    including a missing or empty socket path.

    Returns None -- a disabled session, never "default" -- only when the
    normalized path's own tail is exactly "sessions/<X>/herdr.sock" (that
    is, "sessions" is the third-from-last component, immediately preceding
    the last two) but <X> fails validation: a name containing a path
    separator (so there is no such "sessions" component at that exact
    position at all), or a name that otherwise fails the rule above. Only
    that specific tail shape is considered: an unrelated ancestor directory
    that happens to be named "sessions" (for example
    "/data/sessions/xdg/herdr/herdr.sock", where "sessions" is nowhere near
    the end) does not match it and resolves to "default" instead -- mapping
    it to None would risk disabling a socket path that never named a
    session in the first place.
    """
    if not socket_path:
        return "default"
    normalized = os.path.normpath(str(socket_path))
    parts = normalized.split(os.sep)
    if len(parts) >= 3 and parts[-1] == "herdr.sock" and parts[-3] == "sessions":
        name = parts[-2]
        return name if _valid_session_name(name) else None
    return "default"
