import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from shelf import picker
from shelf.util import FileLock, LockBusy

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
RECORDS = [
    {"id": "b", "archived_at": "2026-09-22T00:00:00Z", "tab": {"label": "fix-retries"}, "workspace": {"label": "api"},
     "panes": {"p": {"agent": "claude", "last_activity": "2026-09-14T12:00:00Z"}}},
    {"id": "a", "archived_at": "2026-09-21T00:00:00Z", "tab": {"label": None}, "workspace": {"label": None},
     "panes": {}},
]


class FakeArchive:
    def __init__(self, records, root=None):
        self.records = list(records)
        self.deleted = []
        self.root = root or Path(tempfile.mkdtemp(prefix="shelf-picker-test-")) / "archive"

    def list(self):
        return list(self.records)

    def delete(self, archive_id):
        self.deleted.append(archive_id)
        self.records = [r for r in self.records if r["id"] != archive_id]


class ParseTest(unittest.TestCase):
    def test_choices(self):
        cases = {"1": ("restore", 0), " 2 ": ("restore", 1), "d1": ("delete", 0), "d 2": ("delete", 1),
                 "q": ("quit", None), "": ("quit", None), "\x1b": ("quit", None), "3": ("invalid", None),
                 "0": ("invalid", None), "dx": ("invalid", None), "hello": ("invalid", None)}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(picker.parse_choice(text, 2), expected)


class RenderTest(unittest.TestCase):
    def test_lines(self):
        lines = picker.render(RECORDS, T0)
        self.assertTrue(lines[0].lstrip().startswith("1"))
        for part in ("fix-retries", "api", "claude", "10d idle"):
            self.assertIn(part, lines[0])
        self.assertIn("(unnamed)", lines[1])

    def test_empty(self):
        self.assertEqual(picker.render([], T0), ["No archived tabs."])

    def test_truncates_to_the_available_width(self):
        rec = {"id": "x", "archived_at": "2026-09-22T00:00:00Z",
               "tab": {"label": "a-very-long-tab-label-that-should-not-fit-in-a-narrow-popup"},
               "workspace": {"label": "a-very-long-workspace-label-that-also-does-not-fit-here"},
               "panes": {"p": {"agent": "claude", "last_activity": "2026-09-14T12:00:00Z"}}}
        lines = picker.render([rec], T0, width=40, height=20)
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]), 39)

    def test_shows_at_most_rows_minus_three_then_a_more_line(self):
        records = [dict(RECORDS[0], id=f"id{i}") for i in range(5)]
        lines = picker.render(records, T0, width=80, height=6)
        self.assertEqual(len(lines), 4)  # 3 entries (rows - 3) + one "more" line
        self.assertIn("2 more", lines[-1])
        self.assertIn("python3 -m shelf list", lines[-1])


class RunTest(unittest.TestCase):
    def run_picker(self, answers, restore_error=None, warnings=()):
        arch = FakeArchive(RECORDS)
        out, restored, notifications = [], [], []
        answers = iter(answers)

        def do_restore(archive_id):
            restored.append(archive_id)
            if restore_error:
                raise restore_error
            return {"tab_id": "t", "warnings": list(warnings)}

        def notify(title, body):
            notifications.append((title, body))

        picker.run(arch, do_restore, lambda: T0, input_fn=lambda prompt: next(answers), print_fn=out.append,
                   notify=notify)
        return arch, restored, out, notifications

    def test_restore_then_exit(self):
        _, restored, _, _ = self.run_picker(["1"])
        self.assertEqual(restored, ["b"])

    def test_esc_quits_without_restoring(self):
        _, restored, _, _ = self.run_picker(["\x1b"])
        self.assertEqual(restored, [])

    def test_delete_asks_for_confirmation_then_deletes(self):
        arch, restored, _, _ = self.run_picker(["d2", "y", "q"])
        self.assertEqual(arch.deleted, ["a"])
        self.assertEqual(restored, [])

    def test_delete_confirmation_prompt_names_the_tab_and_warns_it_is_permanent(self):
        prompts = []
        arch = FakeArchive(RECORDS)
        answers = iter(["d1", "y", "q"])
        picker.run(arch, lambda archive_id: {"tab_id": "t", "warnings": []}, lambda: T0,
                   input_fn=lambda prompt: (prompts.append(prompt), next(answers))[1], print_fn=lambda *_: None)
        self.assertTrue(any("fix-retries" in p and "cannot be undone" in p for p in prompts))

    def test_delete_declined_keeps_the_entry(self):
        arch, restored, _, _ = self.run_picker(["d2", "n", "q"])
        self.assertEqual(arch.deleted, [])
        self.assertEqual(restored, [])

    def test_delete_declined_on_empty_answer(self):
        arch, restored, _, _ = self.run_picker(["d2", "", "q"])
        self.assertEqual(arch.deleted, [])
        self.assertEqual(restored, [])

    def test_delete_runs_under_the_sweep_lock(self):
        # Held for the whole run() call (not just around the FileLock use) so
        # a leftover lock-busy from this test can never bleed into another.
        arch = FakeArchive(RECORDS)
        out = []
        with FileLock(arch.root.parent / "sweep.lock"), \
                mock.patch("shelf.picker.DELETE_LOCK_WAIT_SECONDS", 0.1):
            answers = iter(["d2", "y", "q"])
            picker.run(arch, lambda archive_id: {"tab_id": "t", "warnings": []}, lambda: T0,
                       input_fn=lambda prompt: next(answers), print_fn=out.append)
        self.assertEqual(arch.deleted, [])
        self.assertTrue(any("A sweep is running; try again in a moment." in line for line in out))
        self.assertFalse(any("sweep.lock" in line for line in out))

    def test_restore_lock_busy_shows_friendly_message_and_continues(self):
        arch = FakeArchive(RECORDS)
        lock_path = arch.root.parent / "sweep.lock"

        def do_restore(archive_id):
            raise LockBusy(str(lock_path))

        answers = iter(["1", "q"])
        out = []
        picker.run(arch, do_restore, lambda: T0, input_fn=lambda prompt: next(answers), print_fn=out.append)
        self.assertTrue(any("A sweep is running; try again in a moment." in line for line in out))
        self.assertFalse(any(str(lock_path) in line for line in out))

    def test_invalid_reprompts(self):
        _, restored, out, _ = self.run_picker(["9", "1"])
        self.assertIn("Not a valid choice.", out)
        self.assertEqual(restored, ["b"])

    def test_invalid_choice_message_survives_the_next_render(self):
        # "Not a valid choice." must not be printed until *after* the next
        # render pass, so a screen clear before that render never erases it.
        arch = FakeArchive(RECORDS)
        calls = []
        answers = iter(["9", "q"])
        picker.run(arch, lambda archive_id: {"tab_id": "t", "warnings": []}, lambda: T0,
                   input_fn=lambda prompt: next(answers), print_fn=calls.append)
        self.assertEqual(len(calls), 3)
        self.assertNotIn("Not a valid choice.", calls[0])
        self.assertEqual(calls[2], "Not a valid choice.")

    def test_clears_the_real_screen_via_stdout_write_when_a_tty(self):
        arch = FakeArchive(RECORDS)
        answers = iter(["q"])
        with mock.patch("shelf.picker.sys.stdout") as stdout:
            stdout.isatty.return_value = True
            picker.run(arch, lambda archive_id: {"tab_id": "t", "warnings": []}, lambda: T0,
                       input_fn=lambda prompt: next(answers), print_fn=lambda *_: None)
        stdout.write.assert_called_once_with(picker.CLEAR_SCREEN)

    def test_failed_restore_keeps_picker_open(self):
        _, _, out, _ = self.run_picker(["1", "", "q"], restore_error=RuntimeError("boom"))
        self.assertTrue(any("Restore failed: boom" in line for line in out))

    def test_warnings_are_shown_via_notify_without_waiting_for_input(self):
        _, restored, _, notifications = self.run_picker(["1"], warnings=["conversation missing"])
        self.assertEqual(restored, ["b"])
        self.assertEqual(len(notifications), 1)
        title, body = notifications[0]
        self.assertEqual(title, "shelf")
        self.assertIn("conversation missing", body)


if __name__ == "__main__":
    unittest.main()
