import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from shelf import history


def write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


class ClaudeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_last_real_message_ignores_bookkeeping_meta_and_sidechain(self):
        path = self.home / "projects" / "-src-app" / "S1.jsonl"
        write_jsonl(path, [
            {"type": "user", "timestamp": "2026-09-01T10:00:00Z"},
            {"type": "assistant", "timestamp": "2026-09-02T10:00:00.500Z"},
            {"type": "user", "isMeta": True, "timestamp": "2026-09-20T10:00:00Z"},
            {"type": "assistant", "isSidechain": True, "timestamp": "2026-09-21T10:00:00Z"},
            {"type": "system", "timestamp": "2026-09-22T10:00:00Z"},
            {"type": "cost-state"},
            {"type": "permission-mode"},
        ])
        with open(path, "a") as f:
            f.write("not json\n")
        self.assertEqual(history.last_activity("claude", "S1"),
                         datetime(2026, 9, 2, 10, 0, 0, 500000, tzinfo=timezone.utc))

    def test_missing_session(self):
        self.assertIsNone(history.last_activity("claude", "nope"))

    def test_session_paths_include_companion_dir(self):
        write_jsonl(self.home / "projects" / "-src-app" / "S2.jsonl", [{"type": "user", "timestamp": "2026-09-01T10:00:00Z"}])
        (self.home / "projects" / "-src-app" / "S2" / "tool-results").mkdir(parents=True)
        self.assertEqual([p.name for p in history.claude_session_paths("S2")], ["S2.jsonl", "S2"])

    def test_session_paths_empty_when_missing(self):
        self.assertEqual(history.claude_session_paths("nope"), [])


class CodexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_last_event(self):
        write_jsonl(self.home / "sessions" / "2026" / "07" / "31" / "rollout-2026-07-31T21-50-24-C1.jsonl", [
            {"type": "session_meta", "timestamp": "2026-07-31T21:50:24Z"},
            {"type": "response_item", "timestamp": "2026-07-31T22:00:00Z"},
            {"type": "event_msg", "timestamp": "2026-08-01T05:07:50.770Z"},
            {"type": "turn_context", "timestamp": "2026-08-02T00:00:00Z"},
        ])
        self.assertEqual(history.last_activity("codex", "C1"),
                         datetime(2026, 8, 1, 5, 7, 50, 770000, tzinfo=timezone.utc))


class NoReaderTest(unittest.TestCase):
    def test_agent_without_reader(self):
        self.assertIsNone(history.last_activity("pi", "X"))


if __name__ == "__main__":
    unittest.main()
