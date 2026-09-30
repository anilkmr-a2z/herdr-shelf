"""End to end: the curses loop in a real pseudo-terminal."""

import fcntl
import os
import pty
import select
import shutil
import struct
import tempfile
import termios
import time
import traceback
import unittest
from datetime import datetime, timezone
from pathlib import Path

from shelf import picker

try:
    import curses  # noqa: F401
except ImportError:  # a Python built without curses
    curses = None

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
RECORDS = [
    {"id": "first", "archived_at": "2026-09-23T12:00:00Z", "tab": {"label": "first-tab"}, "workspace": {},
     "panes": {"p": {"agent": "claude", "last_activity": "2026-09-09T12:00:00Z"}}},
    {"id": "second", "archived_at": "2026-09-23T11:00:00Z", "tab": {"label": "second-tab"}, "workspace": {},
     "panes": {"p": {"agent": "claude", "last_activity": "2026-09-09T12:00:00Z"}}},
]


class FakeArchive:
    def __init__(self, root):
        self.root = root

    def list(self):
        return list(RECORDS)

    def delete(self, archive_id):
        pass


@unittest.skipIf(curses is None, "needs curses")
class TtyTest(unittest.TestCase):
    def run_in_pty(self, *chunks, pause=0.2):
        """Run picker.run() in a child on a pty. After the first frame, send each chunk
        `pause` seconds apart. Return (the restored id or None, seconds from the first send to exit)."""
        tmp = Path(tempfile.mkdtemp(prefix="shelf-tty-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        result = tmp / "restored"
        pid, fd = pty.fork()
        if pid == 0:  # child: never return into the test runner
            code = 1
            try:
                os.environ["TERM"] = "xterm-256color"
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 16, 80, 0, 0))

                def do_restore(archive_id):
                    result.write_text(archive_id)
                    return {"warnings": []}

                picker.run(FakeArchive(tmp / "archive"), do_restore, lambda: T0)
                code = 0
            except BaseException:
                traceback.print_exc()
            finally:
                os._exit(code)
        self.addCleanup(os.close, fd)
        output, pending, first_sent, next_send = b"", list(chunks), None, None
        deadline, status = time.monotonic() + 10, None
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                try:
                    output += os.read(fd, 4096)
                except OSError:  # the child exited and closed the pty
                    pass
            if pending and b"first-tab" in output and time.monotonic() >= (next_send or 0):
                os.write(fd, pending.pop(0))
                first_sent = first_sent or time.monotonic()
                next_send = time.monotonic() + pause
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
        else:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            self.fail(f"picker did not exit; output: {output[-500:]!r}")
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, output[-2000:])
        return (result.read_text() if result.exists() else None), time.monotonic() - (first_sent or 0)

    def test_down_then_enter_restores_the_second_tab(self):
        self.assertEqual(self.run_in_pty(b"\x1bOB\r")[0], "second")

    def test_an_undecoded_arrow_is_not_read_as_esc(self):
        self.assertEqual(self.run_in_pty(b"\x1b[B\r")[0], "second")

    def test_the_read_blocks_again_after_an_undecoded_arrow(self):
        self.assertEqual(self.run_in_pty(b"\x1b[B", b"\r")[0], "second")

    def test_one_esc_closes_the_popup(self):
        restored, seconds = self.run_in_pty(b"\x1b")
        self.assertIsNone(restored)
        self.assertLess(seconds, 0.5)  # ncurses' default Esc delay alone is 1 s


if __name__ == "__main__":
    unittest.main()
