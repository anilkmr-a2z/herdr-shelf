import copy
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from shelf import activity, agents, archive, restore
from shelf.api import Client, HerdrError
from shelf.util import FileLock, LockBusy
from tests.fakeherdr import FakeError, FakeHerdr

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
RECORD = {
    "version": 1, "id": "20260920T000000Z-aaaaaa", "archived_at": "2026-09-20T00:00:00Z",
    "workspace": {"label": "api-service", "cwd": "/src/api"},
    "tab": {"label": "fix-retries"},
    "layout": {"focused_pane_id": "w1:p3", "zoomed": False, "root": {
        "type": "split", "direction": "right", "ratio": 0.6,
        "first": {"type": "pane", "pane_id": "w1:p3", "cwd": "/src/api", "command": ["old"]},
        "second": {"type": "pane", "pane_id": "w1:p4", "cwd": "/src/api/logs", "label": "logs",
                   "command": ["tail", "-f", "x"]}}},
    "panes": {
        "w1:p3": {"cwd": "/src/api", "agent": "claude",
                  "session": {"kind": "id", "value": "S1", "source": "herdr:claude"},
                  "launch_argv": ["claude", "--model", "opus"], "last_activity": "2026-09-10T00:00:00Z"},
        "w1:p4": {"cwd": "/src/api/logs"},
    },
    "session_copies": [],
}


class BuildTreeTest(unittest.TestCase):
    def test_agent_gets_resume_command_and_shell_gets_none(self):
        tree = restore.build_tree(RECORD["layout"]["root"], RECORD["panes"], agents.table())
        self.assertEqual((tree["type"], tree["direction"], tree["ratio"]), ("split", "right", 0.6))
        first, second = tree["first"], tree["second"]
        self.assertNotIn("pane_id", first)
        self.assertEqual(first["cwd"], "/src/api")
        self.assertEqual(first["command"], agents.shell_command(["claude", "--model", "opus", "--resume", "S1"]))
        self.assertEqual(second, {"type": "pane", "cwd": "/src/api/logs", "label": "logs"})

    def test_argv_log_collects_the_computed_argv_by_pane_id(self):
        argv_log = {}
        restore.build_tree(RECORD["layout"]["root"], RECORD["panes"], agents.table(), argv_log)
        self.assertEqual(argv_log, {"w1:p3": ["claude", "--model", "opus", "--resume", "S1"]})


class RestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.claude = Path(self.tmp.name) / "claude"
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude)})
        env.start()
        self.addCleanup(env.stop)
        session = self.claude / "projects" / "-src-api" / "S1.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text("{}\n")
        # RECORD's pane cwds ("/src/api", "/src/api/logs") are synthetic and
        # cannot exist for real on the test machine; the missing-cwd warning
        # needs an existing directory as its "happy path" baseline, so pane
        # metadata (not the layout tree, which the warning check ignores) is
        # repointed at a real directory that this test controls.
        self.existing_cwd = str(Path(self.tmp.name) / "work")
        os.mkdir(self.existing_cwd)
        self.state = Path(self.tmp.name) / "state"
        self.arch = archive.Archive(self.state)
        self.arch.save(self._record_with_existing_pane_cwds(), [])
        self.store = activity.ActivityStore(self.state)
        self.fake = FakeHerdr()
        self.addCleanup(self.fake.close)

        def layout_apply(p):
            if "tab_id" in p and "workspace_id" in p:
                raise FakeError("invalid_target", "use either tab_id or workspace_id, not both")
            return {"type": "layout_apply", "layout": {"tab_id": "w1:t7", "workspace_id": p.get("workspace_id")}}

        self.fake.handlers["layout.apply"] = layout_apply
        self.fake.handlers["workspace.list"] = lambda p: {"workspaces": [
            {"workspace_id": "w0", "label": "other"}, {"workspace_id": "w1", "label": "api-service"}]}
        self.fake.handlers["pane.list"] = lambda p: {"panes": []}

    def _record_with_existing_pane_cwds(self, **overrides):
        record = copy.deepcopy(RECORD)
        for meta in record["panes"].values():
            if meta.get("cwd"):
                meta["cwd"] = self.existing_cwd
        record.update(overrides)
        return record

    def run_restore(self):
        return restore.restore(Client(self.fake.path), self.arch, self.store, "20260920T000000Z-aaaaaa", agents.table(), T0)

    def test_restore_logs_before_deleting_the_entry(self):
        with self.assertLogs("shelf", level="INFO") as cm:
            self.run_restore()
        self.assertTrue(any("restored 20260920T000000Z-aaaaaa into w1:t7" in m for m in cm.output))
        self.assertTrue(any("claude:S1:" in m and "--resume" in m and "S1" in m for m in cm.output))

    def test_prompt_like_launch_argv_warns_only_once(self):
        # A launch_argv with a whitespace-containing token looks like it
        # carried a prompt: agents.relaunch_argv logs a warning and falls
        # back to a plain relaunch. That call must happen only once per
        # pane -- once to build the tree and again to log it would double
        # the warning.
        record = self._record_with_existing_pane_cwds(id="20260901T000000Z-dddddd")
        record["panes"]["w1:p3"]["launch_argv"] = ["claude", "fix the bug"]
        self.arch.save(record, [])
        with self.assertLogs("shelf", level="WARNING") as cm:
            restore.restore(Client(self.fake.path), self.arch, self.store, record["id"], agents.table(), T0)
        warnings = [m for m in cm.output if "looked like it carried a prompt" in m]
        self.assertEqual(len(warnings), 1)

    def test_into_existing_workspace(self):
        result = self.run_restore()
        apply = [p for m, p in self.fake.calls if m == "layout.apply"][0]
        self.assertEqual((apply["workspace_id"], apply["tab_label"], apply["focus"]), ("w1", "fix-retries", True))
        self.assertNotIn("tab_id", apply)
        self.assertEqual(result, {"tab_id": "w1:t7", "warnings": []})
        self.assertEqual(self.arch.list(), [])
        self.assertEqual(self.store.load()["claude:S1"]["restored_at"], "2026-09-24T12:00:00Z")

    def test_recreates_missing_workspace_and_replaces_its_first_tab(self):
        self.fake.handlers["workspace.list"] = lambda p: {"workspaces": []}
        self.fake.handlers["workspace.create"] = lambda p: {"type": "workspace_created",
                                                            "workspace": {"workspace_id": "w9", "label": p["label"]},
                                                            "tab": {"tab_id": "w9:t1"}, "root_pane": {"pane_id": "w9:p1"}}
        self.run_restore()
        create = [p for m, p in self.fake.calls if m == "workspace.create"][0]
        self.assertEqual(create, {"label": "api-service", "cwd": "/src/api", "focus": True})
        apply = [p for m, p in self.fake.calls if m == "layout.apply"][0]
        self.assertNotIn("workspace_id", apply)
        self.assertEqual(apply["tab_id"], "w9:t1")

    def test_failed_apply_after_create_closes_the_new_workspace_and_keeps_the_entry(self):
        self.fake.handlers["workspace.list"] = lambda p: {"workspaces": []}
        self.fake.handlers["workspace.create"] = lambda p: {"type": "workspace_created",
                                                            "workspace": {"workspace_id": "w9", "label": p["label"]},
                                                            "tab": {"tab_id": "w9:t1"}, "root_pane": {"pane_id": "w9:p1"}}
        closed = []
        self.fake.handlers["workspace.close"] = lambda p: (closed.append(p["workspace_id"]), {"type": "ok"})[1]

        def fail(p):
            raise FakeError("invalid_layout", "bad")

        self.fake.handlers["layout.apply"] = fail
        with self.assertRaises(HerdrError):
            self.run_restore()
        self.assertEqual(closed, ["w9"])
        self.assertEqual(len(self.arch.list()), 1)

    def test_warns_when_conversation_is_gone(self):
        (self.claude / "projects" / "-src-api" / "S1.jsonl").unlink()
        result = self.run_restore()
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("S1", result["warnings"][0])

    def test_put_back_sessions_avoids_a_false_missing_conversation_warning(self):
        record = self._record_with_existing_pane_cwds(id="20260901T000000Z-bbbbbb",
                                                       session_copies=["projects/-src-api/S1.jsonl"])
        src = self.claude / "projects" / "-src-api" / "S1.jsonl"
        self.arch.save(record, [(src, "projects/-src-api/S1.jsonl")])
        src.unlink()
        self.assertFalse(src.exists())
        result = restore.restore(Client(self.fake.path), self.arch, self.store, record["id"], agents.table(), T0)
        self.assertEqual(result["warnings"], [])
        self.assertTrue(src.exists())

    def test_warns_when_a_pane_cwd_no_longer_exists(self):
        record = self._record_with_existing_pane_cwds(id="20260901T000000Z-cccccc")
        missing = str(Path(self.tmp.name) / "gone")
        record["panes"]["w1:p4"]["cwd"] = missing
        self.arch.save(record, [])
        result = restore.restore(Client(self.fake.path), self.arch, self.store, record["id"], agents.table(), T0)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn(missing, result["warnings"][0])
        self.assertIn("fallback directory", result["warnings"][0])

    def test_failed_apply_keeps_entry(self):
        def fail(p):
            raise FakeError("invalid_layout", "bad")

        self.fake.handlers["layout.apply"] = fail
        with self.assertRaises(HerdrError):
            self.run_restore()
        self.assertEqual(len(self.arch.list()), 1)

    def test_bookkeeping_failure_after_a_successful_apply_still_deletes_the_entry(self):
        with mock.patch("shelf.activity.ActivityStore.update", side_effect=OSError("disk full")):
            result = self.run_restore()
        self.assertEqual(result["tab_id"], "w1:t7")
        self.assertEqual(self.arch.list(), [])

    def test_restore_gives_up_when_a_sweep_already_holds_the_lock(self):
        with FileLock(self.state / "sweep.lock"), \
                mock.patch("shelf.restore.RESTORE_LOCK_WAIT_SECONDS", 0.1):
            with self.assertRaises(LockBusy):
                self.run_restore()
        self.assertNotIn("layout.apply", [m for m, _ in self.fake.calls])

    def test_duplicate_conversation_open_elsewhere_raises_skip_and_keeps_entry(self):
        self.fake.handlers["pane.list"] = lambda p: {"panes": [
            {"pane_id": "w9:p1", "tab_id": "w9:t1", "terminal_id": "term_other", "agent": "claude",
             "agent_session": {"agent": "claude", "kind": "id", "value": "S1", "source": "herdr:claude"}}]}
        with self.assertRaises(archive.Skip) as cm:
            self.run_restore()
        self.assertIn("S1"[:8], str(cm.exception))
        self.assertIn("already open in another tab", str(cm.exception))
        self.assertEqual(len(self.arch.list()), 1)
        self.assertNotIn("layout.apply", [m for m, _ in self.fake.calls])

    def test_unrelated_live_sessions_do_not_block_restore(self):
        self.fake.handlers["pane.list"] = lambda p: {"panes": [
            {"pane_id": "w9:p1", "tab_id": "w9:t1", "terminal_id": "term_other", "agent": "claude",
             "agent_session": {"agent": "claude", "kind": "id", "value": "SOMETHING_ELSE", "source": "herdr:claude"}}]}
        result = self.run_restore()
        self.assertEqual(result["tab_id"], "w1:t7")

    def test_warnings_fall_back_to_workspace_label_or_tab_when_both_are_none(self):
        (self.claude / "projects" / "-src-api" / "S1.jsonl").unlink()

        record = self._record_with_existing_pane_cwds(id="20260901T000000Z-ffffff")
        record["tab"]["label"] = None
        self.arch.save(record, [])
        result = restore.restore(Client(self.fake.path), self.arch, self.store, record["id"], agents.table(), T0)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertTrue(result["warnings"][0].startswith("api-service: "), result["warnings"][0])

        self.fake.handlers["workspace.create"] = lambda p: {"type": "workspace_created",
                                                             "workspace": {"workspace_id": "w9", "label": p.get("label")},
                                                             "tab": {"tab_id": "w9:t1"}, "root_pane": {"pane_id": "w9:p1"}}
        record2 = self._record_with_existing_pane_cwds(id="20260901T000000Z-eeeeee")
        record2["tab"]["label"] = None
        record2["workspace"]["label"] = None
        self.arch.save(record2, [])
        result2 = restore.restore(Client(self.fake.path), self.arch, self.store, record2["id"], agents.table(), T0)
        self.assertEqual(len(result2["warnings"]), 1)
        self.assertTrue(result2["warnings"][0].startswith("tab: "), result2["warnings"][0])


if __name__ == "__main__":
    unittest.main()
