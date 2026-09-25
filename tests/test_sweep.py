import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from shelf import activity, agents, archive, config, sweep
from shelf.api import Client
from shelf.util import FileLock
from tests.fakeherdr import FakeHerdr

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
IDLE = timedelta(days=7)


def pane(pane_id, agent="claude", status="idle", session="S", tab="w1:t1"):
    p = {"pane_id": pane_id, "tab_id": tab, "terminal_id": "term_" + pane_id, "cwd": "/src",
         "agent": agent, "agent_status": status}
    if agent and session:
        p["agent_session"] = {"agent": agent, "kind": "id", "value": session, "source": "herdr:" + agent}
    return p


class DecideTest(unittest.TestCase):
    table = agents.table()

    def decide(self, panes, focused=False, days_ago=10):
        tab = {"tab_id": "w1:t1", "focused": focused}
        activity_of = lambda a, v: None if days_ago is None else T0 - timedelta(days=days_ago)
        return sweep.decide(tab, panes, self.table, activity_of, IDLE, T0)

    def test_cases(self):
        cases = [
            ("idle agent tab", [pane("p1")], {}, None),
            ("blocked counts like idle", [pane("p1", status="blocked")], {}, None),
            ("agent plus shell", [pane("p1"), pane("p2", agent=None)], {}, None),
            ("focused", [pane("p1")], {"focused": True}, "focused"),
            ("working", [pane("p1", status="working")], {}, "working"),
            ("working shell pane", [pane("p1"), pane("p2", agent=None, status="working")], {}, "working"),
            ("shell only", [pane("p1", agent=None)], {}, "no agent pane"),
            ("no session", [pane("p1", session=None)], {}, "no session id"),
            ("unknown agent", [pane("p1", agent="mystery")], {}, "not in the agent table"),
            ("invalid session id", [pane("p1", session="-rf")], {}, "invalid session id"),
            ("recent", [pane("p1")], {"days_ago": 3}, "active 3d ago"),
            ("activity unknown", [pane("p1")], {"days_ago": None}, "activity unknown"),
        ]
        for name, panes, kw, expected in cases:
            with self.subTest(name):
                reason = self.decide(panes, **kw)
                if expected is None:
                    self.assertIsNone(reason)
                else:
                    self.assertIn(expected, reason)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(self.tmp.name, "claude"),
                                           "CODEX_HOME": os.path.join(self.tmp.name, "codex")})
        env.start()
        self.addCleanup(env.stop)
        self.tabs = [{"tab_id": "w1:t1", "workspace_id": "w1", "label": "old", "focused": False},
                     {"tab_id": "w1:t2", "workspace_id": "w1", "label": "here", "focused": True}]
        self.panes = [pane("w1:p1", session="OLD", tab="w1:t1"), pane("w1:p2", session="NOW", tab="w1:t2")]
        self.fake = FakeHerdr()
        self.addCleanup(self.fake.close)
        self.fake.handlers.update({
            "tab.list": lambda p: {"tabs": [dict(t) for t in self.tabs]},
            "pane.list": lambda p: {"panes": [dict(x) for x in self.panes]},
            "notification.show": lambda p: {"type": "ok"},
            "layout.export": lambda p: {"layout": {"workspace_id": "w1", "tab_id": p["tab_id"], "zoomed": False,
                                                   "focused_pane_id": "w1:p1",
                                                   "root": {"type": "pane", "pane_id": "w1:p1", "cwd": "/src"}}},
            "workspace.list": lambda p: {"workspaces": [{"workspace_id": "w1", "label": "main"}]},
            "pane.process_info": lambda p: {"process_info": {"foreground_processes": [{"name": "claude", "argv": ["claude"]}]}},
            "tab.close": self.close_tab,
        })
        activity.ActivityStore(self.state).update(
            lambda d: d.update({"claude:OLD": {"first_seen": "2026-09-01T00:00:00Z"}}))
        self.cfg = config.load(None)

    def close_tab(self, p):
        self.tabs = [t for t in self.tabs if t["tab_id"] != p["tab_id"]]
        self.panes = [x for x in self.panes if x["tab_id"] != p["tab_id"]]
        return {"type": "ok"}

    def run_sweep(self, **kw):
        kw.setdefault("now", T0)
        return sweep.run(Client(self.fake.path), self.cfg, self.state, agents.table(), **kw)

    def test_dry_run_reports_without_closing(self):
        report = self.run_sweep()
        self.assertEqual(report["eligible"], ["old"])
        self.assertNotIn("tab.close", self.fake.methods())
        notes = [p for m, p in self.fake.calls if m == "notification.show"]
        self.assertEqual(notes, [{"title": "shelf", "body": "shelf (dry-run): would archive 1 tab: old"}])

    def test_new_sessions_get_first_seen_and_are_not_archived(self):
        self.run_sweep()
        self.assertEqual(activity.ActivityStore(self.state).load()["claude:NOW"]["first_seen"], "2026-09-24T12:00:00Z")

    def test_blocked_is_not_refreshed_by_the_sweep(self):
        self.panes[0]["agent_status"] = "blocked"
        report = self.run_sweep()
        self.assertEqual(report["eligible"], ["old"])
        self.assertNotIn("last_active", activity.ActivityStore(self.state).load()["claude:OLD"])

    def test_working_is_refreshed_by_the_sweep(self):
        self.panes[0]["agent_status"] = "working"
        self.run_sweep()
        self.assertEqual(activity.ActivityStore(self.state).load()["claude:OLD"]["last_active"], "2026-09-24T12:00:00Z")

    def test_live_archives(self):
        self.cfg["mode"] = "live"
        report = self.run_sweep()
        self.assertEqual(report["archived"], ["old"])
        self.assertIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)
        self.assertEqual(len(archive.Archive(self.state).list()), 1)

    def test_live_resolves_the_tab_again_by_terminal(self):
        self.cfg["mode"] = "live"
        original = self.fake.handlers["tab.list"]
        count = {"n": 0}

        def renumbering(p):
            count["n"] += 1
            if count["n"] == 2:
                for t in self.tabs:
                    if t["tab_id"] == "w1:t1":
                        t["tab_id"] = "w1:t9"
                for x in self.panes:
                    if x["tab_id"] == "w1:t1":
                        x["tab_id"] = "w1:t9"
            return original(p)

        self.fake.handlers["tab.list"] = renumbering
        self.run_sweep()
        self.assertIn(("tab.close", {"tab_id": "w1:t9"}), self.fake.calls)
        self.assertNotIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)

    def test_if_due_respects_interval(self):
        self.assertIsNotNone(self.run_sweep(if_due=True))
        self.assertIsNone(self.run_sweep(if_due=True, now=T0 + timedelta(minutes=30)))
        self.assertIsNotNone(self.run_sweep(if_due=True, now=T0 + timedelta(minutes=61)))

    def test_busy_lock_skips(self):
        with FileLock(self.state / "sweep.lock"):
            self.assertIsNone(self.run_sweep())

    def test_archive_now_ignores_idle_days(self):
        self.panes[0]["agent_session"]["value"] = "NEVER_SEEN"
        archive_id = sweep.archive_now(Client(self.fake.path), self.cfg, self.state, agents.table(), "w1:t1", now=T0)
        self.assertTrue(archive_id)
        self.assertIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)

    def test_archive_now_refuses_focused_and_missing(self):
        with self.assertRaises(archive.Skip):
            sweep.archive_now(Client(self.fake.path), self.cfg, self.state, agents.table(), "w1:t2", now=T0)
        with self.assertRaises(archive.Skip):
            sweep.archive_now(Client(self.fake.path), self.cfg, self.state, agents.table(), "w1:t404", now=T0)


class SummaryTest(unittest.TestCase):
    def test_formats(self):
        empty = {"mode": "dry-run", "eligible": [], "archived": [], "failed": [], "skipped": []}
        self.assertIsNone(sweep.summary(empty))
        live = {"mode": "live", "eligible": ["a", "b", "c"], "archived": ["a", "c"], "failed": [("b", "x")], "skipped": []}
        self.assertEqual(sweep.summary(live), "shelf: archived 2 tabs: a, c; 1 failed, see herdr plugin log")


if __name__ == "__main__":
    unittest.main()
