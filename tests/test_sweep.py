import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from shelf import activity, agents, archive, config, sweep
from shelf.api import Client, HerdrError
from shelf.util import FileLock, iso
from tests.fakeherdr import FakeError, FakeHerdr

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

    def decide(self, panes, focused=False, days_ago=10, open_in=None):
        tab = {"tab_id": "w1:t1", "focused": focused}
        activity_of = lambda a, v, terminal_id=None: None if days_ago is None else T0 - timedelta(days=days_ago)
        return sweep.decide(tab, panes, self.table, activity_of, IDLE, T0, open_in=open_in)

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

    def test_agent_mismatched_with_its_session_is_not_eligible(self):
        # herdr can keep a previous agent's session on a pane where a
        # different agent now runs; that pane's own agent is "codex" but the
        # session it's carrying still says "claude".
        p = pane("p1")
        p["agent"] = "codex"
        reason = self.decide([p])
        self.assertIn("agent does not match its session", reason)

    def test_terminal_id_is_forwarded_to_activity_of(self):
        seen = {}

        def activity_of(agent, value, terminal_id=None):
            seen["terminal_id"] = terminal_id
            return T0 - timedelta(days=10)

        tab = {"tab_id": "w1:t1", "focused": False}
        reason = sweep.decide(tab, [pane("p1")], self.table, activity_of, IDLE, T0)
        self.assertIsNone(reason)
        self.assertEqual(seen["terminal_id"], "term_p1")

    def test_duplicate_conversation_open_in_another_tab_is_not_eligible(self):
        reason = self.decide([pane("p1", session="S")], open_in={"claude:S": {"w1:t1", "w1:t9"}})
        self.assertIn("also open in another tab", reason)
        self.assertIn("S"[:8], reason)

    def test_conversation_only_in_its_own_tab_is_eligible(self):
        reason = self.decide([pane("p1", session="S")], open_in={"claude:S": {"w1:t1"}})
        self.assertIsNone(reason)

    def test_open_in_defaulting_to_none_keeps_old_behavior(self):
        reason = self.decide([pane("p1", session="S")])
        self.assertIsNone(reason)

    def test_duplicate_conversation_in_two_panes_of_the_same_tab_is_not_eligible(self):
        # Two panes in the *same* tab carrying the same session: open_in's
        # set (keyed by tab_id) never grows past size 1 for this tab alone,
        # so this must be caught independently of the open_in map.
        panes = [pane("p1", session="S"), pane("p2", session="S")]
        reason = self.decide(panes)
        self.assertIn("also open in another pane", reason)


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
            "layout.export": self.layout_export,
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

    def layout_export(self, p):
        pane_id = next((x["pane_id"] for x in self.panes if x["tab_id"] == p["tab_id"]), None)
        return {"layout": {"workspace_id": "w1", "tab_id": p["tab_id"], "zoomed": False,
                            "focused_pane_id": pane_id, "root": {"type": "pane", "pane_id": pane_id, "cwd": "/src"}}}

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

    def test_live_skips_a_target_that_becomes_ineligible_before_closing(self):
        self.cfg["mode"] = "live"
        original = self.fake.handlers["tab.list"]
        count = {"n": 0}

        def flips_focused(p):
            count["n"] += 1
            if count["n"] == 2:
                for t in self.tabs:
                    if t["tab_id"] == "w1:t1":
                        t["focused"] = True
            return original(p)

        self.fake.handlers["tab.list"] = flips_focused
        report = self.run_sweep()
        self.assertEqual(report["archived"], [])
        self.assertNotIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)

    def test_live_isolates_a_failed_target_and_still_archives_the_rest(self):
        self.cfg["mode"] = "live"
        self.tabs.append({"tab_id": "w1:t3", "workspace_id": "w1", "label": "also-old", "focused": False})
        self.panes.append(pane("w1:p3", session="OLD3", tab="w1:t3"))
        activity.ActivityStore(self.state).update(
            lambda d: d.update({"claude:OLD3": {"first_seen": "2026-09-01T00:00:00Z"}}))
        original_close = self.close_tab

        def close_tab(p):
            if p["tab_id"] == "w1:t1":
                raise FakeError("definite_fail", "nope")
            return original_close(p)

        self.fake.handlers["tab.close"] = close_tab
        report = self.run_sweep()
        self.assertEqual(sorted(report["eligible"]), ["also-old", "old"])
        self.assertEqual(report["archived"], ["also-old"])
        self.assertEqual([label for label, _ in report["failed"]], ["old"])
        notes = [p for m, p in self.fake.calls if m == "notification.show"]
        self.assertEqual(notes[-1]["body"], "shelf: archived 1 tab: also-old; 1 failed, see herdr plugin log")

    def test_if_due_is_rechecked_after_acquiring_the_lock(self):
        self.cfg["mode"] = "live"
        with mock.patch("shelf.sweep.is_due", side_effect=[True, False]):
            result = self.run_sweep(if_due=True)
        self.assertIsNone(result)
        self.assertNotIn("tab.close", self.fake.methods())

    def test_partial_sweep_still_notifies_once_when_a_later_targets_gather_fails(self):
        self.cfg["mode"] = "live"
        self.tabs.append({"tab_id": "w1:t3", "workspace_id": "w1", "label": "also-old", "focused": False})
        self.panes.append(pane("w1:p3", session="OLD3", tab="w1:t3"))
        activity.ActivityStore(self.state).update(
            lambda d: d.update({"claude:OLD3": {"first_seen": "2026-09-01T00:00:00Z"}}))
        original = self.fake.handlers["tab.list"]
        count = {"n": 0}

        def flaky(p):
            count["n"] += 1
            if count["n"] == 3:  # the second target's per-target re-gather
                raise FakeError("definite_fail", "gone")
            return original(p)

        self.fake.handlers["tab.list"] = flaky
        report = self.run_sweep()
        self.assertEqual(report["archived"], ["old"])
        self.assertEqual([label for label, _ in report["failed"]], ["also-old"])
        notes = [p for m, p in self.fake.calls if m == "notification.show"]
        self.assertEqual(len(notes), 1)

    def test_a_single_targets_unexpected_exception_does_not_abort_the_sweep(self):
        self.cfg["mode"] = "live"
        self.tabs.append({"tab_id": "w1:t3", "workspace_id": "w1", "label": "also-old", "focused": False})
        self.panes.append(pane("w1:p3", session="OLD3", tab="w1:t3"))
        activity.ActivityStore(self.state).update(
            lambda d: d.update({"claude:OLD3": {"first_seen": "2026-09-01T00:00:00Z"}}))
        with mock.patch("shelf.archive.archive_tab", side_effect=[ValueError("boom"), "dummy-id"]):
            report = self.run_sweep()
        self.assertEqual([label for label, _ in report["failed"]], ["old"])
        self.assertEqual(report["archived"], ["also-old"])

    def test_last_sweep_written_after_a_successful_gather(self):
        self.run_sweep()
        self.assertTrue((self.state / "last_sweep").exists())

    def test_last_sweep_not_written_when_the_initial_gather_fails(self):
        def fail(p):
            raise FakeError("unavailable", "no herdr")

        self.fake.handlers["tab.list"] = fail
        with self.assertRaises(HerdrError):
            self.run_sweep()
        self.assertFalse((self.state / "last_sweep").exists())

    def test_last_sweep_written_even_if_sweep_raises_after_a_successful_gather(self):
        with mock.patch("shelf.sweep._record_presence", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.run_sweep()
        self.assertTrue((self.state / "last_sweep").exists())

    def test_skip_reason_is_logged(self):
        with self.assertLogs("shelf", level="INFO") as cm:
            self.run_sweep()
        self.assertTrue(any("skip here: focused" in message for message in cm.output))

    def test_installed_at_is_written_once_and_never_rewritten(self):
        self.run_sweep(now=T0)
        first = (self.state / "installed_at").read_text()
        self.run_sweep(now=T0 + timedelta(hours=2))
        second = (self.state / "installed_at").read_text()
        self.assertEqual(first, second)

    def test_workspace_list_failure_falls_back_to_no_workspace_labels(self):
        # Tab "old" already has its own (non-numeric) label, so it is
        # reported normally even though workspace.list itself failed.
        def fail(p):
            raise FakeError("unavailable", "no herdr")

        self.fake.handlers["workspace.list"] = fail
        report = self.run_sweep()
        self.assertEqual(report["eligible"], ["old"])

    def test_workspace_list_failure_still_reports_a_numeric_label_as_is(self):
        self.tabs.append({"tab_id": "w1:t3", "workspace_id": "w1", "label": "7", "focused": False})
        self.panes.append(pane("w1:p3", session="OLD3", tab="w1:t3"))
        activity.ActivityStore(self.state).update(
            lambda d: d.update({"claude:OLD3": {"first_seen": "2026-09-01T00:00:00Z"}}))

        def fail(p):
            raise FakeError("unavailable", "no herdr")

        self.fake.handlers["workspace.list"] = fail
        report = self.run_sweep()
        self.assertEqual(sorted(report["eligible"]), ["7", "old"])

    def test_numeric_or_missing_tab_label_falls_back_to_workspace_label(self):
        self.tabs.append({"tab_id": "w1:t3", "workspace_id": "w1", "label": "7", "focused": False})
        self.panes.append(pane("w1:p3", session="OLD3", tab="w1:t3"))
        activity.ActivityStore(self.state).update(
            lambda d: d.update({"claude:OLD3": {"first_seen": "2026-09-01T00:00:00Z"}}))
        report = self.run_sweep()
        self.assertEqual(sorted(report["eligible"]), ["main/7", "old"])

    def test_empty_tab_label_falls_back_to_workspace_label(self):
        self.tabs.append({"tab_id": "w1:t3", "workspace_id": "w1", "label": "", "focused": False})
        self.panes.append(pane("w1:p3", session="OLD3", tab="w1:t3"))
        activity.ActivityStore(self.state).update(
            lambda d: d.update({"claude:OLD3": {"first_seen": "2026-09-01T00:00:00Z"}}))
        report = self.run_sweep()
        self.assertIn("main/w1:t3", report["eligible"])

    def test_recent_agent_start_on_the_terminal_keeps_an_old_session_ineligible(self):
        # claude:OLD's own history/first_seen is over idle_days old (as in
        # test_live_archives), but the pane's terminal shows an agent started
        # 20 minutes ago (a hand resume, or herdr restarting the agent):
        # that must count as activity for the tab as a whole.
        session_file = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "-src" / "OLD.jsonl"
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text(json.dumps({"type": "user", "timestamp": iso(T0 - timedelta(days=31))}) + "\n")
        terminal_id = self.panes[0]["terminal_id"]

        def set_started(d):
            d.setdefault("terminals", {})[terminal_id] = {"agent_started_at": iso(T0 - timedelta(minutes=20))}
            return True

        activity.ActivityStore(self.state).update(set_started)
        report = self.run_sweep()
        self.assertNotIn("old", report["eligible"])
        reasons = dict(report["skipped"])
        self.assertIn("active 0d ago", reasons["old"])

    def test_two_tabs_sharing_one_session_are_both_left_alone(self):
        self.tabs.append({"tab_id": "w1:t3", "workspace_id": "w1", "label": "dup", "focused": False})
        self.panes.append(pane("w1:p3", session="OLD", tab="w1:t3"))
        report = self.run_sweep()
        self.assertNotIn("old", report["eligible"])
        self.assertNotIn("dup", report["eligible"])
        reasons = dict(report["skipped"])
        self.assertIn("also open in another tab", reasons["old"])
        self.assertIn("also open in another tab", reasons["dup"])

    def test_agent_started_at_survives_a_terminal_id_change_from_a_restart(self):
        # A start recorded 2 hours ago under the pane's original terminal id.
        old_terminal = self.panes[0]["terminal_id"]

        def record_old_start(d):
            d.setdefault("terminals", {})[old_terminal] = {"agent_started_at": iso(T0 - timedelta(hours=2))}
            return True

        activity.ActivityStore(self.state).update(record_old_start)
        self.run_sweep()  # copies the terminal's start onto claude:OLD's own session record
        # herdr restarts: the pane now has a brand new terminal id, and the
        # old terminal's entry (if not already copied) would be pruned.
        self.panes[0]["terminal_id"] = "term_after_restart"
        report = self.run_sweep(now=T0 + timedelta(minutes=5))
        self.assertNotIn("old", report["eligible"])

    def test_live_recheck_catches_a_duplicate_that_appears_during_the_sweep(self):
        # The duplicate does not exist yet at the initial gather -- only a
        # stale open_in map (built once, up front) would miss it.
        self.cfg["mode"] = "live"
        original = self.fake.handlers["tab.list"]
        count = {"n": 0}

        def late_duplicate(p):
            count["n"] += 1
            if count["n"] == 2:  # "old"'s own per-target re-gather
                self.tabs.append({"tab_id": "w1:t9", "workspace_id": "w1", "label": "dup", "focused": False})
                self.panes.append(pane("w1:p9", session="OLD", tab="w1:t9"))
            return original(p)

        self.fake.handlers["tab.list"] = late_duplicate
        report = self.run_sweep()
        self.assertEqual(report["archived"], [])
        self.assertNotIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)
        reasons = dict(report["skipped"])
        self.assertIn("also open in another tab", reasons["old"])

    def test_archive_now_warns_but_proceeds_when_the_session_is_open_elsewhere(self):
        self.tabs.append({"tab_id": "w1:t3", "workspace_id": "w1", "label": "dup", "focused": False})
        self.panes.append(pane("w1:p3", session="OLD", tab="w1:t3"))
        with self.assertLogs("shelf", level="WARNING") as cm:
            archive_id = sweep.archive_now(Client(self.fake.path), self.cfg, self.state, agents.table(),
                                           "w1:t1", now=T0)
        self.assertTrue(archive_id)
        self.assertIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)
        self.assertTrue(any("also open in another tab" in m for m in cm.output))


class RecordPresenceTest(unittest.TestCase):
    def test_working_pane_sets_last_status_so_a_later_done_event_counts(self):
        # last_status "done" is stale here: track() never saw this session go
        # to "working" (for example the plugin was not running then), so
        # without this fix a following "done" would look like no change.
        data = {"claude:S": {"first_seen": "2026-09-01T00:00:00Z", "last_status": "done"}}
        tabs = [({}, [pane("p1", status="working", session="S")])]
        sweep._record_presence(data, tabs, T0)
        self.assertEqual(data["claude:S"]["last_status"], "working")
        self.assertTrue(activity.record_status(data, "claude:S", "done", T0 + timedelta(minutes=5)))

    def test_terminal_entries_for_vanished_terminals_are_pruned(self):
        data = {"terminals": {"term_gone": {"agent_started_at": "2026-09-01T00:00:00Z"},
                               "term_p1": {"agent_started_at": "2026-09-20T00:00:00Z"}}}
        tabs = [({}, [pane("p1", session="S")])]  # only "term_p1" is still present
        self.assertTrue(sweep._record_presence(data, tabs, T0))
        self.assertNotIn("term_gone", data["terminals"])
        self.assertIn("term_p1", data["terminals"])

    def test_no_terminals_key_is_untouched_when_nothing_vanished(self):
        data = {}
        tabs = [({}, [pane("p1", session="S")])]
        sweep._record_presence(data, tabs, T0)
        self.assertNotIn("terminals", data)

    def test_terminal_start_is_copied_onto_the_sessions_own_record(self):
        # So it survives a herdr restart, which assigns the pane a new
        # terminal_id and would otherwise prune the only copy of this.
        data = {"terminals": {"term_p1": {"agent_started_at": "2026-09-20T00:00:00Z"}}}
        tabs = [({}, [pane("p1", session="S")])]
        self.assertTrue(sweep._record_presence(data, tabs, T0))
        self.assertEqual(data["claude:S"]["agent_started_at"], "2026-09-20T00:00:00Z")

    def test_copying_the_terminal_start_never_regresses_a_later_value(self):
        data = {"claude:S": {"agent_started_at": "2026-09-23T00:00:00Z"},
                "terminals": {"term_p1": {"agent_started_at": "2026-09-20T00:00:00Z"}}}
        tabs = [({}, [pane("p1", session="S")])]
        sweep._record_presence(data, tabs, T0)
        self.assertEqual(data["claude:S"]["agent_started_at"], "2026-09-23T00:00:00Z")

    def test_pruning_keeps_a_terminal_started_at_or_after_this_sweeps_now(self):
        # A concurrent track() call could record a brand new terminal id for
        # a pane that was not part of this sweep's own tabs snapshot;
        # pruning must not delete it just because it postdates that snapshot.
        data = {"terminals": {"term_new": {"agent_started_at": iso(T0)}}}
        tabs = [({}, [pane("p1", session="S")])]  # "term_new" isn't among these
        sweep._record_presence(data, tabs, T0)
        self.assertIn("term_new", data["terminals"])

    def test_pruning_removes_a_terminal_started_well_before_this_sweep(self):
        data = {"terminals": {"term_old": {"agent_started_at": iso(T0 - timedelta(days=5))}}}
        tabs = [({}, [pane("p1", session="S")])]
        sweep._record_presence(data, tabs, T0)
        self.assertNotIn("term_old", data["terminals"])

    def test_non_dict_terminals_value_is_reset(self):
        data = {"terminals": [], "claude:S": {"first_seen": "2026-09-01T00:00:00Z"}}
        tabs = [({}, [pane("p1", session="S")])]
        changed = sweep._record_presence(data, tabs, T0)
        self.assertTrue(changed)
        self.assertEqual(data["terminals"], {})

    def test_non_dict_terminal_entry_is_skipped_not_crashed(self):
        data = {"terminals": {"t": "x"}, "claude:S": {"first_seen": "2026-09-01T00:00:00Z"}}
        tabs = [({}, [pane("p1", session="S")])]  # pane's own terminal_id "term_p1" differs from "t"
        sweep._record_presence(data, tabs, T0)  # must not raise
        self.assertNotIn("t", data["terminals"])


class ActivityLookupTest(unittest.TestCase):
    def test_non_dict_terminals_value_is_ignored(self):
        activity_of = sweep._activity_lookup({"terminals": []}, None)
        self.assertIsNone(activity_of("claude", "S", "term1"))

    def test_non_dict_terminal_entry_is_ignored(self):
        activity_of = sweep._activity_lookup({"terminals": {"term1": "x"}}, None)
        self.assertIsNone(activity_of("claude", "S", "term1"))


class InstalledAtEligibilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir(parents=True)
        self.claude = Path(self.tmp.name) / "claude"
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude)})
        env.start()
        self.addCleanup(env.stop)
        self.fake = FakeHerdr()
        self.addCleanup(self.fake.close)
        self.tabs = [
            {"tab_id": "w1:t1", "workspace_id": "w1", "label": "seen-at-install", "focused": False},
            {"tab_id": "w1:t2", "workspace_id": "w1", "label": "seen-after-install", "focused": False},
        ]
        self.panes = [pane("w1:p1", session="A", tab="w1:t1"), pane("w1:p2", session="B", tab="w1:t2")]
        self.fake.handlers.update({
            "tab.list": lambda p: {"tabs": [dict(t) for t in self.tabs]},
            "pane.list": lambda p: {"panes": [dict(x) for x in self.panes]},
            "notification.show": lambda p: {"type": "ok"},
            "workspace.list": lambda p: {"workspaces": [{"workspace_id": "w1", "label": "main"}]},
        })
        old_ts = iso(T0 - timedelta(days=30))
        for session_id in ("A", "B"):
            f = self.claude / "projects" / "-src" / f"{session_id}.jsonl"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"type": "user", "timestamp": old_ts}) + "\n")
        (self.state / "installed_at").write_text(iso(T0) + "\n")
        activity.ActivityStore(self.state).update(lambda d: d.update({
            "claude:A": {"first_seen": iso(T0)},  # seen at the very first sweep: installed_at itself
            "claude:B": {"first_seen": iso(T0 + timedelta(days=9))},  # seen well after install
        }))
        self.cfg = config.load(None)

    def test_first_seen_after_install_is_not_eligible_but_at_install_is(self):
        now = T0 + timedelta(days=10)
        report = sweep.run(Client(self.fake.path), self.cfg, self.state, agents.table(), now=now)
        self.assertIn("seen-at-install", report["eligible"])
        self.assertNotIn("seen-after-install", report["eligible"])


class SummaryTest(unittest.TestCase):
    def test_formats(self):
        empty = {"mode": "dry-run", "eligible": [], "archived": [], "failed": [], "skipped": []}
        self.assertIsNone(sweep.summary(empty))
        live = {"mode": "live", "eligible": ["a", "b", "c"], "archived": ["a", "c"], "failed": [("b", "x")], "skipped": []}
        self.assertEqual(sweep.summary(live), "shelf: archived 2 tabs: a, c; 1 failed, see herdr plugin log")


if __name__ == "__main__":
    unittest.main()
