import copy
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from shelf import activity, agents, archive, restore
from shelf.api import Client, HerdrError
from tests.fakeherdr import FakeError, FakeHerdr

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
RECORD = {
    "version": 1, "id": "r1", "archived_at": "2026-09-20T00:00:00Z",
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
        self.state = Path(self.tmp.name) / "state"
        self.arch = archive.Archive(self.state)
        self.arch.save(copy.deepcopy(RECORD), [])
        self.store = activity.ActivityStore(self.state)
        self.fake = FakeHerdr()
        self.addCleanup(self.fake.close)
        self.fake.handlers["layout.apply"] = lambda p: {"type": "layout_apply", "layout": {
            "tab_id": "w1:t7", "workspace_id": p.get("workspace_id")}}
        self.fake.handlers["workspace.list"] = lambda p: {"workspaces": [
            {"workspace_id": "w0", "label": "other"}, {"workspace_id": "w1", "label": "api-service"}]}

    def run_restore(self):
        return restore.restore(Client(self.fake.path), self.arch, self.store, "r1", agents.table(), T0)

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
        self.assertEqual((apply["workspace_id"], apply["tab_id"]), ("w9", "w9:t1"))

    def test_warns_when_conversation_is_gone(self):
        (self.claude / "projects" / "-src-api" / "S1.jsonl").unlink()
        result = self.run_restore()
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("S1", result["warnings"][0])

    def test_failed_apply_keeps_entry(self):
        def fail(p):
            raise FakeError("invalid_layout", "bad")

        self.fake.handlers["layout.apply"] = fail
        with self.assertRaises(HerdrError):
            self.run_restore()
        self.assertEqual(len(self.arch.list()), 1)


if __name__ == "__main__":
    unittest.main()
