"""The archive-tab popup: the question it shows, and reading one key."""

from __future__ import annotations

import os
import termios
import textwrap
import tty

# Text columns: the manifest's popup width (64) less the border, the leading
# space and one spare column.
WIDTH = 60
HINT = "The tab closes; the restore picker brings it back."
KEYS = "y archive   any other key cancel"


def _printable(text: str) -> str:
    # Any process can set a tab label; a control character in one (an escape
    # sequence, say) printed raw could redraw the question.
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def lines(preview: dict) -> list:
    """The popup's lines for a sweep.preview() result, wrapped to WIDTH."""
    text = [f'Archive "{preview["label"]}"?', preview["activity"], *preview["warnings"], HINT, KEYS]
    return [" " + piece for line in text for piece in textwrap.wrap(_printable(line), WIDTH)]


def read_key(fd: int) -> bytes:
    """One byte from the terminal, read in raw mode; the terminal's previous
    mode is restored on every path.

    tty.setraw flushes input first (TCSAFLUSH), so a key typed before the
    question was drawn is dropped rather than taken as the answer.
    """
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return os.read(fd, 1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
