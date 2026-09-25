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
        "pane.list": lambda p: {"panes": [dict(x) for x in PANES]},
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
        self.activity_of = lambda agent, value, terminal_id=None: datetime(2026, 9, 10, tzinfo=timezone.utc)

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
        # pid5's argv[1:] is a superset of pid9's -- pid9 (the smaller tail)
        # is the outermost, user-typed command under the subset rule.
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

    def test_no_outermost_process_when_matches_are_unrelated(self):
        # Neither candidate's tail is a subset of the other's: an unrelated
        # "claude mcp serve" child also matches the program name, but it is
        # not a wrapper around the interactive claude process.
        fake = fake_herdr(self)
        fake.handlers["pane.process_info"] = lambda p: {"process_info": {"foreground_processes": [
            {"pid": 3, "name": "claude", "argv": ["claude", "--model", "opus"]},
            {"pid": 4, "name": "claude", "argv": ["claude", "mcp", "serve"]}]}}
        record, _ = archive.capture(Client(fake.path), TAB, PANES, self.table, self.activity_of, False, T0)
        self.assertIsNone(record["panes"]["w1:p3"]["launch_argv"])

    def test_numeric_tab_label_becomes_none(self):
        fake = fake_herdr(self)
        tab = dict(TAB, label="7")
        record, _ = archive.capture(Client(fake.path), tab, PANES, self.table, self.activity_of, False, T0)
        self.assertIsNone(record["tab"]["label"])

    def test_layout_pane_ids_mismatch_raises_skip(self):
        fake = fake_herdr(self)
        fake.handlers["layout.export"] = lambda p: {"layout": {
            "workspace_id": "w1", "tab_id": "w1:t2", "zoomed": False, "focused_pane_id": "w1:p3",
            "root": {"type": "pane", "pane_id": "w1:p999", "cwd": "/src/api"}}}
        with self.assertRaises(archive.Skip):
            archive.capture(Client(fake.path), TAB, PANES, self.table, self.activity_of, True, T0)

    def test_layout_with_too_many_panes_raises_skip(self):
        fake = fake_herdr(self)
        pane_ids = [f"w1:p{i}" for i in range(1, 26)]  # one more than herdr's 24-pane limit

        def balanced(nodes):
            if len(nodes) == 1:
                return nodes[0]
            mid = len(nodes) // 2
            return {"type": "split", "direction": "right", "ratio": 0.5,
                    "first": balanced(nodes[:mid]), "second": balanced(nodes[mid:])}

        root = balanced([{"type": "pane", "pane_id": pid, "cwd": "/src"} for pid in pane_ids])
        fake.handlers["layout.export"] = lambda p: {"layout": {
            "workspace_id": "w1", "tab_id": "w1:t2", "zoomed": False,
            "focused_pane_id": pane_ids[0], "root": root}}
        panes = [{"pane_id": pid, "tab_id": "w1:t2", "terminal_id": f"term_{pid}", "cwd": "/src"}
                 for pid in pane_ids]
        with self.assertRaises(archive.Skip):
            archive.capture(Client(fake.path), TAB, panes, self.table, self.activity_of, False, T0)

    def test_layout_deeper_than_16_raises_skip(self):
        fake = fake_herdr(self)

        def chain(n, i=1):
            # A split whose first side is always a leaf and whose second side
            # nests one level deeper, so depth grows by 1 per pane without
            # needing many panes to exceed the 16-deep limit.
            if n == 1:
                return {"type": "pane", "pane_id": f"w1:p{i}", "cwd": "/src"}
            return {"type": "split", "direction": "right", "ratio": 0.5,
                    "first": {"type": "pane", "pane_id": f"w1:p{i}", "cwd": "/src"},
                    "second": chain(n - 1, i + 1)}

        root = chain(17)  # one deeper than herdr's 16-deep limit
        pane_ids = [f"w1:p{i}" for i in range(1, 18)]
        fake.handlers["layout.export"] = lambda p: {"layout": {
            "workspace_id": "w1", "tab_id": "w1:t2", "zoomed": False,
            "focused_pane_id": pane_ids[0], "root": root}}
        panes = [{"pane_id": pid, "tab_id": "w1:t2", "terminal_id": f"term_{pid}", "cwd": "/src"}
                 for pid in pane_ids]
        with self.assertRaises(archive.Skip):
            archive.capture(Client(fake.path), TAB, panes, self.table, self.activity_of, False, T0)

    def test_layout_with_exactly_24_panes_is_allowed(self):
        fake = fake_herdr(self)
        pane_ids = [f"w1:p{i}" for i in range(1, 25)]  # exactly herdr's 24-pane limit

        def balanced(nodes):
            if len(nodes) == 1:
                return nodes[0]
            mid = len(nodes) // 2
            return {"type": "split", "direction": "right", "ratio": 0.5,
                    "first": balanced(nodes[:mid]), "second": balanced(nodes[mid:])}

        root = balanced([{"type": "pane", "pane_id": pid, "cwd": "/src"} for pid in pane_ids])
        fake.handlers["layout.export"] = lambda p: {"layout": {
            "workspace_id": "w1", "tab_id": "w1:t2", "zoomed": False,
            "focused_pane_id": pane_ids[0], "root": root}}
        panes = [{"pane_id": pid, "tab_id": "w1:t2", "terminal_id": f"term_{pid}", "cwd": "/src"}
                 for pid in pane_ids]
        record, _ = archive.capture(Client(fake.path), TAB, panes, self.table, self.activity_of, False, T0)
        self.assertEqual(len(record["panes"]), 24)

    def test_layout_with_exactly_depth_16_is_allowed(self):
        fake = fake_herdr(self)

        def chain(n, i=1):
            if n == 1:
                return {"type": "pane", "pane_id": f"w1:p{i}", "cwd": "/src"}
            return {"type": "split", "direction": "right", "ratio": 0.5,
                    "first": {"type": "pane", "pane_id": f"w1:p{i}", "cwd": "/src"},
                    "second": chain(n - 1, i + 1)}

        root = chain(16)  # exactly herdr's 16-deep limit
        pane_ids = [f"w1:p{i}" for i in range(1, 17)]
        fake.handlers["layout.export"] = lambda p: {"layout": {
            "workspace_id": "w1", "tab_id": "w1:t2", "zoomed": False,
            "focused_pane_id": pane_ids[0], "root": root}}
        panes = [{"pane_id": pid, "tab_id": "w1:t2", "terminal_id": f"term_{pid}", "cwd": "/src"}
                 for pid in pane_ids]
        record, _ = archive.capture(Client(fake.path), TAB, panes, self.table, self.activity_of, False, T0)
        self.assertEqual(len(record["panes"]), 16)


class ArchiveTabTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(self.tmp.name, "claude")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.arch = archive.Archive(Path(self.tmp.name) / "state")
        self.table = agents.table()
        self.activity_of = lambda agent, value, terminal_id=None: None

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

    def test_lost_reply_but_tab_confirmed_gone_keeps_the_record(self):
        # tab.close's reply never arrived, but the close actually happened
        # server-side: pane.list shows the tab's terminals are gone, so the
        # record -- the only surviving copy -- must be kept.
        fake = fake_herdr(self)
        real = Client(fake.path)
        state = {"closed": False}
        fake.handlers["pane.list"] = lambda p: {"panes": [] if state["closed"] else [dict(x) for x in PANES]}

        class LostReply:
            def call(self, method, params=None):
                if method == "tab.close":
                    state["closed"] = True
                    raise HerdrError("io", "reply lost", definite=False)
                return real.call(method, params)

        with self.assertRaises(HerdrError):
            archive.archive_tab(LostReply(), self.arch, TAB, PANES, self.table, self.activity_of, True, T0)
        self.assertEqual(len(self.arch.list()), 1)

    def test_lost_reply_but_tab_confirmed_still_open_deletes_the_record(self):
        # tab.close's reply never arrived, and pane.list shows the tab is
        # still there with exactly its original terminals: the close never
        # took effect, so the record is a useless duplicate and is removed.
        fake = fake_herdr(self)
        real = Client(fake.path)

        class LostReply:
            def call(self, method, params=None):
                if method == "tab.close":
                    raise HerdrError("io", "reply lost", definite=False)
                return real.call(method, params)

        with self.assertRaises(HerdrError):
            archive.archive_tab(LostReply(), self.arch, TAB, PANES, self.table, self.activity_of, True, T0)
        self.assertEqual(self.arch.list(), [])

    def test_pane_list_mismatch_before_close_skips_and_deletes_record(self):
        fake = fake_herdr(self)
        fake.handlers["pane.list"] = lambda p: {"panes": [
            {"pane_id": "w1:p3", "tab_id": "w1:t2", "terminal_id": "term_other", "cwd": "/src/api"}]}
        with self.assertRaises(archive.Skip):
            archive.archive_tab(Client(fake.path), self.arch, TAB, PANES, self.table, self.activity_of, True, T0)
        self.assertEqual(self.arch.list(), [])
        self.assertNotIn("tab.close", fake.methods())

    def test_pane_list_raising_before_close_deletes_record_and_reraises(self):
        fake = fake_herdr(self)

        def raise_err(p):
            raise FakeError("unavailable", "boom")

        fake.handlers["pane.list"] = raise_err
        with self.assertRaises(HerdrError):
            archive.archive_tab(Client(fake.path), self.arch, TAB, PANES, self.table, self.activity_of, True, T0)
        self.assertEqual(self.arch.list(), [])
        self.assertNotIn("tab.close", fake.methods())

    def test_layout_mismatch_is_skipped_before_saving_anything(self):
        fake = fake_herdr(self)
        fake.handlers["layout.export"] = lambda p: {"layout": {
            "workspace_id": "w1", "tab_id": "w1:t2", "zoomed": False, "focused_pane_id": "w1:p3",
            "root": {"type": "pane", "pane_id": "w1:p999", "cwd": "/src/api"}}}
        with self.assertRaises(archive.Skip):
            archive.archive_tab(Client(fake.path), self.arch, TAB, PANES, self.table, self.activity_of, True, T0)
        self.assertEqual(self.arch.list(), [])


class StoreTest(unittest.TestCase):
    def test_list_newest_first_and_delete(self):
        with tempfile.TemporaryDirectory() as d:
            arch = archive.Archive(d)
            arch.save({"id": "20260901T000000Z-aaaaaa", "archived_at": "2026-09-01T00:00:00Z"}, [])
            arch.save({"id": "20260902T000000Z-bbbbbb", "archived_at": "2026-09-02T00:00:00Z"}, [])
            self.assertEqual([r["id"] for r in arch.list()],
                              ["20260902T000000Z-bbbbbb", "20260901T000000Z-aaaaaa"])
            arch.delete("20260902T000000Z-bbbbbb")
            self.assertEqual([r["id"] for r in arch.list()], ["20260901T000000Z-aaaaaa"])
            with self.assertRaises(KeyError):
                arch.load("20260902T000000Z-bbbbbb")

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
                rec = {"id": "20260901T000000Z-cccccc", "archived_at": "2026-09-01T00:00:00Z",
                       "session_copies": ["projects/-x/S.jsonl", "projects/-x/S"]}
                arch.save(rec, [(src, "projects/-x/S.jsonl"), (companion, "projects/-x/S")])
                self.assertEqual(arch.put_back_sessions(rec), [])
                src.unlink()
                shutil.rmtree(companion)
                self.assertEqual(arch.put_back_sessions(rec), ["projects/-x/S.jsonl", "projects/-x/S"])
                self.assertEqual(src.read_text(), "original\n")
                self.assertEqual((companion / "f.txt").read_text(), "f")

    def test_delete_tolerates_a_missing_record_json(self):
        with tempfile.TemporaryDirectory() as d:
            arch = archive.Archive(d)
            arch.save({"id": "20260901T000000Z-aaaaaa", "archived_at": "2026-09-01T00:00:00Z"}, [])
            (Path(d) / "archive" / "20260901T000000Z-aaaaaa" / "record.json").unlink()
            arch.delete("20260901T000000Z-aaaaaa")  # must not raise
            self.assertFalse((Path(d) / "archive" / "20260901T000000Z-aaaaaa").exists())

    def test_delete_of_an_invalid_id_raises_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            arch = archive.Archive(d)
            arch.save({"id": "20260901T000000Z-aaaaaa", "archived_at": "2026-09-01T00:00:00Z"}, [])
            with self.assertRaises(KeyError):
                arch.delete("..")
            self.assertEqual([r["id"] for r in arch.list()], ["20260901T000000Z-aaaaaa"])
            self.assertTrue((Path(d) / "archive" / "20260901T000000Z-aaaaaa").exists())

    def test_put_back_sessions_ignores_path_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            claude = Path(d) / "claude"
            claude.mkdir()
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude)}):
                arch = archive.Archive(Path(d) / "state")
                rec = {"id": "20260901T000000Z-dddddd", "archived_at": "2026-09-01T00:00:00Z",
                       "session_copies": ["../escaped", "/abs/escaped"]}
                arch.save(rec, [])
                # A real file sitting exactly where "../escaped" would
                # actually read from, so the test fails (something gets
                # copied out) if the traversal guard is ever removed. The
                # "sessions" directory must exist too: the OS needs to
                # traverse through it before it can apply "..".
                folder = Path(d) / "state" / "archive" / rec["id"]
                (folder / "sessions").mkdir(parents=True, exist_ok=True)
                (folder / "escaped").write_text("leaked")
                self.assertEqual(arch.put_back_sessions(rec), [])
                self.assertFalse((Path(d) / "escaped").exists())
                self.assertFalse(Path("/abs/escaped").exists())

    def test_save_removes_partial_folder_on_failure(self):
        with tempfile.TemporaryDirectory() as d:
            arch = archive.Archive(d)
            rec = {"id": "20260901T000000Z-eeeeee", "archived_at": "2026-09-01T00:00:00Z"}
            src = Path(d) / "src.jsonl"
            src.write_text("data")
            with mock.patch("shutil.copy2", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    arch.save(rec, [(src, "projects/-x/S.jsonl")])
            self.assertFalse((Path(d) / "archive" / rec["id"]).exists())

    def test_id_with_trailing_newline_is_rejected(self):
        # re.match's "$" matches just before a trailing newline, so a
        # match()-based check would incorrectly accept this id and let save()
        # write a real (oddly-named) entry; fullmatch must reject it outright.
        with tempfile.TemporaryDirectory() as d:
            arch = archive.Archive(d)
            with self.assertRaises(KeyError):
                arch.save({"id": "20260901T000000Z-aaaaaa\n", "archived_at": "2026-09-01T00:00:00Z"}, [])
            self.assertEqual(arch.list(), [])

    def test_delete_removes_record_first_so_listing_is_unaffected_by_a_failed_rmtree(self):
        with tempfile.TemporaryDirectory() as d:
            arch = archive.Archive(d)
            arch.save({"id": "20260901T000000Z-ffffff", "archived_at": "2026-09-01T00:00:00Z"}, [])
            with mock.patch("shutil.rmtree") as rmtree:
                arch.delete("20260901T000000Z-ffffff")  # must not raise even though rmtree below is a no-op
            rmtree.assert_called_once()
            self.assertFalse((Path(d) / "archive" / "20260901T000000Z-ffffff" / "record.json").exists())
            self.assertEqual(arch.list(), [])


if __name__ == "__main__":
    unittest.main()
