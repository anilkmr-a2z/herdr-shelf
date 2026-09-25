import unittest
from datetime import datetime, timezone

from shelf import picker

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
RECORDS = [
    {"id": "b", "archived_at": "2026-09-22T00:00:00Z", "tab": {"label": "fix-retries"}, "workspace": {"label": "api"},
     "panes": {"p": {"agent": "claude", "last_activity": "2026-09-14T12:00:00Z"}}},
    {"id": "a", "archived_at": "2026-09-21T00:00:00Z", "tab": {"label": None}, "workspace": {"label": None},
     "panes": {}},
]


class FakeArchive:
    def __init__(self, records):
        self.records = list(records)
        self.deleted = []

    def list(self):
        return list(self.records)

    def delete(self, archive_id):
        self.deleted.append(archive_id)
        self.records = [r for r in self.records if r["id"] != archive_id]


class ParseTest(unittest.TestCase):
    def test_choices(self):
        cases = {"1": ("restore", 0), " 2 ": ("restore", 1), "d1": ("delete", 0), "d 2": ("delete", 1),
                 "q": ("quit", None), "": ("quit", None), "3": ("invalid", None), "0": ("invalid", None),
                 "dx": ("invalid", None), "hello": ("invalid", None)}
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


class RunTest(unittest.TestCase):
    def run_picker(self, answers, restore_error=None, warnings=()):
        arch = FakeArchive(RECORDS)
        out, restored = [], []
        answers = iter(answers)

        def do_restore(archive_id):
            restored.append(archive_id)
            if restore_error:
                raise restore_error
            return {"tab_id": "t", "warnings": list(warnings)}

        picker.run(arch, do_restore, lambda: T0, input_fn=lambda prompt: next(answers), print_fn=out.append)
        return arch, restored, out

    def test_restore_then_exit(self):
        _, restored, _ = self.run_picker(["1"])
        self.assertEqual(restored, ["b"])

    def test_delete_then_quit(self):
        arch, restored, _ = self.run_picker(["d2", "q"])
        self.assertEqual(arch.deleted, ["a"])
        self.assertEqual(restored, [])

    def test_invalid_reprompts(self):
        _, restored, out = self.run_picker(["9", "1"])
        self.assertIn("Not a valid choice.", out)
        self.assertEqual(restored, ["b"])

    def test_failed_restore_keeps_picker_open(self):
        _, _, out = self.run_picker(["1", "", "q"], restore_error=RuntimeError("boom"))
        self.assertTrue(any("Restore failed: boom" in line for line in out))

    def test_warnings_are_shown(self):
        _, _, out = self.run_picker(["1", ""], warnings=["conversation missing"])
        self.assertIn("conversation missing", out)


if __name__ == "__main__":
    unittest.main()
