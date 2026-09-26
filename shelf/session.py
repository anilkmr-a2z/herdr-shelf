"""Which herdr session a socket path belongs to.

Each herdr session has its own socket: the default session's is
"<herdr config dir>/herdr.sock", and a named session <name>'s is
"<herdr config dir>/sessions/<name>/herdr.sock". Plugin commands get
HERDR_SOCKET_PATH for the session they run in, so the session name is
recovered from that path alone.
"""

from __future__ import annotations

import re

_NAMED_SESSION_RE = re.compile(r"/sessions/([^/]+)/herdr\.sock/*$")


def herdr_session_name(socket_path) -> str:
    """The herdr session name for a HERDR_SOCKET_PATH value.

    Returns the <name> in ".../sessions/<name>/herdr.sock" (trailing
    slashes tolerated). Anything else -- including a missing or empty
    socket path -- is the default session.
    """
    if not socket_path:
        return "default"
    match = _NAMED_SESSION_RE.search(str(socket_path))
    return match.group(1) if match else "default"
