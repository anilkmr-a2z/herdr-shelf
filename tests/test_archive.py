import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from shelf import agents, archive
from shelf.api import Client, HerdrError
from tests.fakeherdr import FakeError, FakeHerdr

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
TAB = {"tab_id": "w1:t2", "workspace_id": "w1", "label": "fix-retries", "focused": False}
PANES = [
    {"pane_id": "w1:p3", "tab_id": "w1:t2", "terminal_id": "term_a", "cwd": "/src/api", "agent": "claude",
     "agent_status": "idle", "agent_session": {"agent": "claude", "kind": "id", "value": "S1", "source": "herdr:claude"}},
    {"pane_id": "w1:p4", "tab_id": "w1:t2", "terminal_id": "term_b", "cwd": "/src/api/logs", "agent": None,
     "agent_status": "idle"},
]
LAYOUT = {"workspace_id": "w1", "tab_id": "w1:t2", "zoomed": False, "focused_pane_id": "w1:p3",
          "root": {"type": "split", "direction": "right", "ratio": 0.6,
                   "first": {"type": "pane", "pane_id": "w1:p3", "cwd": "/src/api"},
                   "second": {"type": "pane", "pane_id": "w1:p4", "cwd": "/src/api/logs", "label": "logs"}}}


def fake_herdr(test, argv=("/usr/bin/claude", "--model", "opus")):
    fake = FakeHerdr()
    test.addCleanup(fake.close)
    fake.handlers.update({
        "layout.export": lambda p: {"type": "layout_export", "layout": LAYOUT},
        "workspace.list": lambda p: {"workspaces": [{"workspace_id": "w1", "label": "api-service"}]},
        "pane.process_info": lambda p: {"process_info": {"foreground_processes": [
            {"pid": 1, "name": "bash", "argv": ["bash"]},
            {"pid": 2, "name": "claude", "argv": list(argv)}]}},
        "tab.close": lambda p: {"type": "ok"},
    })
    return fake


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.claude = Path(self.tmp.name) / "claude"
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude)})
        patcher.start()
        self.addCleanup(patcher.stop)
        session = self.claude / "projects" / "-src-api" / "S1.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text('{"type":"user","timestamp":"2026-09-10T00:00:00Z"}\n')
        (self.claude / "projects" / "-src-api" / "S1").mkdir()
        self.table = agents.table()
        self.activity_of = lambda agent, value: datetime(2026, 9, 10, tzinfo=timezone.utc)

    def test_record_contents(self):
        fake = fake_herdr(self)
        record, files = archive.capture(Client(fake.path), TAB, PANES, self.table, self.activity_of, True, T0)
        self.assertEqual(record["workspace"], {"label": "api-service", "cwd": "/src/api"})
        self.assertEqual(record["tab"], {"label": "fix-retries"})
        self.assertEqual(record["layout"]["root"], LAYOUT["root"])
        claude = record["panes"]["w1:p3"]
        self.assertEqual(claude["launch_argv"], ["/usr/bin/claude", "--model", "opus"])
        self.assertEqual(claude["session"], {"kind": "id", "value": "S1", "source": "herdr:claude"})
        self.assertEqual(claude["last_activity"], "2026-09-10T00:00:00Z")
        self.assertEqual(record["panes"]["w1:p4"], {"cwd": "/src/api/logs"})
        self.assertEqual(record["session_copies"], ["projects/-src-api/S1.jsonl", "projects/-src-api/S1"])
        self.assertEqual(len(files), 2)
        self.assertTrue(record["id"].startswith("20260924T120000Z-"))

    def test_outermost_matching_process_wins(self):
        fake = fake_herdr(self)
        fake.handlers["pane.process_info"] = lambda p: {"process_info": {"foreground_processes": [
            {"pid": 5, "name": "claude", "argv": ["/opt/claude/2.1/claude", "--settings", "{\"a\": 1}", "--model", "opus"]},
            {"pid": 9, "name": "claude", "argv": ["/usr/local/bin/claude", "--model", "opus"]}]}}
        record, _ = archive.capture(Client(fake.path), TAB, PANES, self.table, self.activity_of, False, T0)
        self.assertEqual(record["panes"]["w1:p3"]["launch_argv"], ["/usr/local/bin/claude", "--model", "opus"])

    def test_no_matching_process_means_no_launch_argv(self):
        fake = fake_herdr(self)
        fake.handlers["pane.process_info"] = lambda p: {"process_info": {"foreground_processes": [
            {"pid": 2, "name": "node", "argv": ["node", "/opt/cli.js"]}]}}
        record, _ = archive.capture(Client(fake.path), TAB, PANES, self.table, self.activity_of, False, T0)
        self.assertIsNone(record["panes"]["w1:p3"]["launch_argv"])
        self.assertEqual(record["session_copies"], [])


class ArchiveTabTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(self.tmp.name, "claude")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.arch = archive.Archive(Path(self.tmp.name) / "state")
        self.table = agents.table()
        self.activity_of = lambda agent, value: None

    def test_record_written_before_close(self):
        fake = fake_herdr(self)
        seen = {}

        def close(p):
            seen["records_at_close"] = len(self.arch.list())
            return {"type": "ok"}

        fake.handlers["tab.close"] = close
        archive_id = archive.archive_tab(Client(fake.path), self.arch, TAB, PANES, self.table, self.activity_of, True, T0)
        self.assertEqual(seen["records_at_close"], 1)
        self.assertEqual(self.arch.load(archive_id)["tab"]["label"], "fix-retries")
        self.assertEqual(fake.methods()[-1], "tab.close")

    def test_worktree_group_skips_and_cleans_up(self):
        fake = fake_herdr(self)

        def refuse(p):
            raise FakeError("confirmation_required", "worktree group")

        fake.handlers["tab.close"] = refuse
        with self.assertRaises(archive.Skip):
            archive.archive_tab(Client(fake.path), self.arch, TAB, PANES, self.table, self.activity_of, True, T0)
        self.assertEqual(self.arch.list(), [])

    def test_unknown_close_outcome_keeps_the_record(self):
        fake = fake_herdr(self)
        real = Client(fake.path)

        class LostReply:
            def call(self, method, params=None):
                if method == "tab.close":
                    raise HerdrError("io", "reply lost", definite=False)
                return real.call(method, params)

        with self.assertRaises(HerdrError):
            archive.archive_tab(LostReply(), self.arch, TAB, PANES, self.table, self.activity_of, True, T0)
        self.assertEqual(len(self.arch.list()), 1)


class StoreTest(unittest.TestCase):
    def test_list_newest_first_and_delete(self):
        with tempfile.TemporaryDirectory() as d:
            arch = archive.Archive(d)
            arch.save({"id": "a", "archived_at": "2026-09-01T00:00:00Z"}, [])
            arch.save({"id": "b", "archived_at": "2026-09-02T00:00:00Z"}, [])
            self.assertEqual([r["id"] for r in arch.list()], ["b", "a"])
            arch.delete("b")
            self.assertEqual([r["id"] for r in arch.list()], ["a"])
            with self.assertRaises(KeyError):
                arch.load("b")

    def test_put_back_sessions_only_when_missing(self):
        with tempfile.TemporaryDirectory() as d:
            claude = Path(d) / "claude"
            src = claude / "projects" / "-x" / "S.jsonl"
            src.parent.mkdir(parents=True)
            src.write_text("original\n")
            companion = claude / "projects" / "-x" / "S"
            companion.mkdir()
            (companion / "f.txt").write_text("f")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude)}):
                arch = archive.Archive(Path(d) / "state")
                rec = {"id": "r1", "archived_at": "2026-09-01T00:00:00Z",
                       "session_copies": ["projects/-x/S.jsonl", "projects/-x/S"]}
                arch.save(rec, [(src, "projects/-x/S.jsonl"), (companion, "projects/-x/S")])
                self.assertEqual(arch.put_back_sessions(rec), [])
                src.unlink()
                shutil.rmtree(companion)
                self.assertEqual(arch.put_back_sessions(rec), ["projects/-x/S.jsonl", "projects/-x/S"])
                self.assertEqual(src.read_text(), "original\n")
                self.assertEqual((companion / "f.txt").read_text(), "f")


if __name__ == "__main__":
    unittest.main()
