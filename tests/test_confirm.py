import os
import pty
import termios
import threading
import time
import unittest
from unittest import mock

from shelf import confirm

PREVIEW = {"tab_id": "w1:t1", "label": "api-refactor", "terminals": ["t1"], "blocks": [], "warnings": [],
           "activity": "Last activity 3 days ago."}


class LinesTest(unittest.TestCase):
    def test_clean_tab(self):
        self.assertEqual(confirm.lines(PREVIEW), [
            ' Archive "api-refactor"?',
            " Last activity 3 days ago.",
            " The tab closes; the restore picker brings it back.",
            " y archive   any other key cancel",
        ])

    def test_warnings_come_between_the_activity_line_and_the_hint(self):
        preview = dict(PREVIEW, warnings=["A pane is still working; archiving stops it."])
        self.assertEqual(confirm.lines(preview)[2], " A pane is still working; archiving stops it.")
        self.assertEqual(len(confirm.lines(preview)), 5)

    def test_control_characters_in_a_label_are_not_printed(self):
        out = confirm.lines(dict(PREVIEW, label="api\x1b[2Jrefactor"))
        self.assertEqual(out[0], ' Archive "api?[2Jrefactor"?')

    def test_a_long_warning_wraps_within_the_popup(self):
        warning = "Conversation 12345678 is also open in another tab; restore waits until that copy is closed."
        out = confirm.lines(dict(PREVIEW, warnings=[warning]))
        self.assertEqual(len(out), 6)
        self.assertTrue(all(len(line) <= confirm.WIDTH + 1 for line in out))


def _mode(fd):
    """tcgetattr(fd) without PENDIN, a state bit macOS sets on its own when a
    terminal goes back to canonical mode."""
    mode = termios.tcgetattr(fd)
    mode[3] &= ~getattr(termios, "PENDIN", 0)
    return mode


class ReadKeyTest(unittest.TestCase):
    def pty(self):
        parent, child = pty.openpty()
        # Cleanups run last-in, first-out: parent closes first, so a reader
        # still blocked on child wakes up while child is open.
        self.addCleanup(os.close, child)
        self.addCleanup(os.close, parent)
        # Nothing reads the parent side here, so echo would leave output
        # pending, and on macOS TCSAFLUSH waits for pending output to drain.
        mode = termios.tcgetattr(child)
        mode[3] &= ~termios.ECHO
        termios.tcsetattr(child, termios.TCSANOW, mode)
        return parent, child

    def read_key_while_typing(self, before, after):
        """read_key() on a pty, with `before` typed ahead and `after` typed
        once it is in raw mode."""
        parent, child = self.pty()
        mode = _mode(child)
        if before:
            os.write(parent, before)
            # Linux hands pty input to the line discipline from a work queue,
            # and TCSAFLUSH only flushes what has arrived there.
            time.sleep(0.05)
        result = []
        reader = threading.Thread(target=lambda: result.append(confirm.read_key(child)), daemon=True)
        reader.start()
        deadline = time.monotonic() + 2
        # tty.setraw flushes input before it clears ICANON, so once ICANON is
        # clear, anything written now is read rather than flushed.
        while termios.tcgetattr(child)[3] & termios.ICANON:
            self.assertLess(time.monotonic(), deadline, "read_key never entered raw mode")
            time.sleep(0.01)
        self.assertFalse(termios.tcgetattr(child)[3] & (termios.ICANON | termios.ISIG))
        os.write(parent, after)
        reader.join(2)
        self.assertFalse(reader.is_alive(), "read_key did not return")
        self.assertEqual(_mode(child), mode)
        return result[0]

    def test_reads_one_byte_and_restores_the_terminal_mode(self):
        self.assertEqual(self.read_key_while_typing(b"", b"yz"), b"y")

    def test_escape_is_read_as_its_own_byte(self):
        self.assertEqual(self.read_key_while_typing(b"", b"\x1b[B"), b"\x1b")

    def test_a_key_typed_before_the_question_is_dropped(self):
        self.assertEqual(self.read_key_while_typing(b"y", b"n"), b"n")

    def test_the_mode_is_restored_when_the_read_fails(self):
        _, child = self.pty()
        mode = _mode(child)
        with mock.patch("shelf.confirm.os.read", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                confirm.read_key(child)
        self.assertEqual(_mode(child), mode)


if __name__ == "__main__":
    unittest.main()
