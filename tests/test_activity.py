import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from shelf import activity
from shelf.api import Client
from tests.fakeherdr import FakeHerdr

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class RecordTest(unittest.TestCase):
    def test_touch_sets_first_seen_and_last_active(self):
        d = {}
        self.assertTrue(activity.touch(d, "claude:S", T0))
        self.assertEqual(d["claude:S"], {"first_seen": "2026-09-24T12:00:00Z", "last_active": "2026-09-24T12:00:00Z"})

    def test_touch_skips_within_60_seconds(self):
        d = {}
        activity.touch(d, "k", T0)
        self.assertFalse(activity.touch(d, "k", T0 + timedelta(seconds=30)))
        self.assertTrue(activity.touch(d, "k", T0 + timedelta(seconds=61)))
        self.assertEqual(d["k"]["last_active"], "2026-09-24T12:01:01Z")

    def test_see_only_sets_first_seen_once(self):
        d = {}
        self.assertTrue(activity.see(d, "k", T0))
        self.assertFalse(activity.see(d, "k", T0 + timedelta(days=1)))
        self.assertEqual(d["k"], {"first_seen": "2026-09-24T12:00:00Z"})

    def test_mark_restored(self):
        d = {}
        activity.mark_restored(d, "k", T0)
        self.assertEqual(d["k"]["restored_at"], "2026-09-24T12:00:00Z")

    def test_touch_overwrites_a_last_active_that_is_in_the_future(self):
        d = {"k": {"first_seen": stamp(T0), "last_active": stamp(T0 + timedelta(hours=1))}}
        # Without the clock-skew guard, "now - last_active" is negative and
        # smaller than the 60s skip window, so the stale future value would stick.
        self.assertTrue(activity.touch(d, "k", T0))
        self.assertEqual(d["k"]["last_active"], "2026-09-24T12:00:00Z")


class RecordStatusTest(unittest.TestCase):
    def test_done_then_done_again_does_not_move_last_active(self):
        d = {}
        self.assertTrue(activity.record_status(d, "k", "done", T0))
        first_active = d["k"]["last_active"]
        self.assertFalse(activity.record_status(d, "k", "done", T0 + timedelta(seconds=120)))
        self.assertEqual(d["k"]["last_active"], first_active)
        self.assertEqual(d["k"]["last_status"], "done")

    def test_done_working_done_counts_both_done_calls(self):
        d = {}
        self.assertTrue(activity.record_status(d, "k", "done", T0))
        self.assertEqual(d["k"]["last_active"], stamp(T0))
        t1 = T0 + timedelta(seconds=120)
        self.assertTrue(activity.record_status(d, "k", "working", t1))
        self.assertEqual(d["k"]["last_active"], stamp(t1))
        t2 = t1 + timedelta(seconds=120)
        self.assertTrue(activity.record_status(d, "k", "done", t2))
        self.assertEqual(d["k"]["last_active"], stamp(t2))

    def test_blocked_then_blocked_again_does_not_move_last_active(self):
        d = {}
        self.assertTrue(activity.record_status(d, "k", "blocked", T0))
        first_active = d["k"]["last_active"]
        self.assertFalse(activity.record_status(d, "k", "blocked", T0 + timedelta(seconds=120)))
        self.assertEqual(d["k"]["last_active"], first_active)

    def test_working_always_counts_subject_to_the_60s_skip(self):
        d = {}
        self.assertTrue(activity.record_status(d, "k", "working", T0))
        self.assertFalse(activity.record_status(d, "k", "working", T0 + timedelta(seconds=30)))
        self.assertTrue(activity.record_status(d, "k", "working", T0 + timedelta(seconds=61)))


class EffectiveTest(unittest.TestCase):
    def test_history_replaces_first_seen(self):
        rec = {"first_seen": stamp(T0)}
        self.assertEqual(activity.effective(rec, T0 - timedelta(days=30)), T0 - timedelta(days=30))

    def test_first_seen_used_without_history(self):
        self.assertEqual(activity.effective({"first_seen": stamp(T0)}, None), T0)

    def test_latest_wins(self):
        rec = {"first_seen": stamp(T0 - timedelta(days=20)), "last_active": stamp(T0 - timedelta(days=9)),
               "restored_at": stamp(T0 - timedelta(days=2))}
        self.assertEqual(activity.effective(rec, T0 - timedelta(days=15)), T0 - timedelta(days=2))

    def test_nothing_known(self):
        self.assertIsNone(activity.effective({}, None))

    def test_first_seen_after_install_counts_even_with_history(self):
        # A conversation resumed by hand after install: history is old, but
        # the plugin only just noticed the session, so that counts as activity.
        installed_at = T0 - timedelta(days=1)
        rec = {"first_seen": stamp(T0)}
        history_ts = T0 - timedelta(days=30)
        self.assertEqual(activity.effective(rec, history_ts, installed_at), T0)

    def test_first_seen_at_install_time_does_not_count_over_history(self):
        # Every session already open on the first sweep gets
        # first_seen == installed_at; for those, history alone decides.
        installed_at = T0
        rec = {"first_seen": stamp(T0)}
        history_ts = T0 - timedelta(days=30)
        self.assertEqual(activity.effective(rec, history_ts, installed_at), history_ts)

    def test_first_seen_without_installed_at_is_ignored_when_history_exists(self):
        rec = {"first_seen": stamp(T0)}
        history_ts = T0 - timedelta(days=30)
        self.assertEqual(activity.effective(rec, history_ts), history_ts)

    def test_first_seen_alone_is_unaffected_by_installed_at_when_there_is_no_history(self):
        installed_at = T0
        rec = {"first_seen": stamp(T0 - timedelta(days=5))}
        self.assertEqual(activity.effective(rec, None, installed_at), T0 - timedelta(days=5))

    def test_agent_started_at_counts_unconditionally_like_last_active(self):
        # Old history/first_seen, but the agent was (re)started 20 minutes
        # ago: that must count regardless of what history says, the same way
        # last_active and restored_at do.
        rec = {"first_seen": stamp(T0 - timedelta(days=30)), "agent_started_at": stamp(T0 - timedelta(minutes=20))}
        history_ts = T0 - timedelta(days=31)
        self.assertEqual(activity.effective(rec, history_ts, T0), T0 - timedelta(minutes=20))


class StoreAndTrackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = activity.ActivityStore(self.tmp.name)
        self.fake = FakeHerdr()
        self.addCleanup(self.fake.close)
        self.fake.handlers["pane.get"] = lambda p: {"pane": {"pane_id": p["pane_id"], "agent_session": {
            "agent": "claude", "kind": "id", "value": "S", "source": "herdr:claude"}}}

    def event(self, status):
        # herdr's real envelope: the "event" field (and "data.type") are
        # snake_case EventKind names, not the dot-form hook name.
        return json.dumps({"event": "pane_agent_status_changed",
                            "data": {"type": "pane_agent_status_changed", "pane_id": "w1:p1",
                                     "workspace_id": "w1", "agent_status": status, "agent": "claude"}})

    def test_working_is_recorded(self):
        self.assertTrue(activity.track(Client(self.fake.path), self.store, self.event("working"), None, T0))
        self.assertEqual(self.store.load()["claude:S"]["last_active"], "2026-09-24T12:00:00Z")

    def test_idle_and_unknown_are_ignored(self):
        for status in ("idle", "unknown"):
            self.assertFalse(activity.track(Client(self.fake.path), self.store, self.event(status), None, T0))
        self.assertEqual(self.store.load(), {})
        self.assertEqual(self.fake.calls, [])

    def test_pane_without_session_is_ignored(self):
        self.fake.handlers["pane.get"] = lambda p: {"pane": {"pane_id": p["pane_id"]}}
        self.assertFalse(activity.track(Client(self.fake.path), self.store, self.event("done"), None, T0))
        self.assertEqual(self.store.load(), {})

    def test_pane_id_falls_back_to_env(self):
        ev = json.dumps({"event": "pane_agent_status_changed",
                          "data": {"type": "pane_agent_status_changed", "agent_status": "blocked"}})
        self.assertTrue(activity.track(Client(self.fake.path), self.store, ev, "w1:p9", T0))
        self.assertEqual(self.fake.calls, [("pane.get", {"pane_id": "w1:p9"})])

    def test_garbage_event_is_ignored(self):
        self.assertFalse(activity.track(Client(self.fake.path), self.store, "not json", None, T0))
        self.assertFalse(activity.track(Client(self.fake.path), self.store, None, None, T0))

    def test_non_dict_data_is_ignored(self):
        ev = json.dumps({"event": "pane_agent_status_changed", "data": ["nope"]})
        self.assertFalse(activity.track(Client(self.fake.path), self.store, ev, None, T0))
        self.assertEqual(self.store.load(), {})
        self.assertEqual(self.fake.calls, [])

    def test_non_dict_agent_session_is_ignored(self):
        self.fake.handlers["pane.get"] = lambda p: {"pane": {"pane_id": p["pane_id"], "agent_session": "claude:S"}}
        self.assertFalse(activity.track(Client(self.fake.path), self.store, self.event("done"), None, T0))
        self.assertEqual(self.store.load(), {})

    def test_return_value_reflects_whether_the_store_actually_changed(self):
        client = Client(self.fake.path)
        self.assertTrue(activity.track(client, self.store, self.event("working"), None, T0))
        # Same session, same status, well within the 60s skip: no write happens.
        self.assertFalse(activity.track(client, self.store, self.event("working"), None, T0 + timedelta(seconds=1)))


class AgentDetectedTrackTest(unittest.TestCase):
    """track() handling pane.agent_detected: an agent (re)started in a pane
    counts as activity, keyed by the pane's terminal. Event shapes match
    herdr's real payload: the JSON envelope's own "event" (and "data.type")
    are the snake_case EventKind name "pane_agent_detected", not the dot-form
    hook name -- that dot form only ever appears in HERDR_PLUGIN_EVENT
    (env_event), passed to track() separately.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = activity.ActivityStore(self.tmp.name)
        self.fake = FakeHerdr()
        self.addCleanup(self.fake.close)
        self.fake.handlers["pane.get"] = lambda p: {"pane": {"pane_id": p["pane_id"], "terminal_id": "term1"}}
        # Retries are exercised by call count, not wall-clock time.
        delay_patcher = mock.patch("shelf.activity.AGENT_SESSION_RETRY_DELAY_SECONDS", 0)
        delay_patcher.start()
        self.addCleanup(delay_patcher.stop)

    def event(self, agent="claude", released=False, pane_id="w1:p1", include_agent=True):
        # herdr omits "released" entirely when it is false, and omits "agent"
        # on a release -- a released event never carries an agent name.
        data = {"type": "pane_agent_detected", "pane_id": pane_id, "workspace_id": "w1"}
        if include_agent:
            data["agent"] = agent
        if released:
            data["released"] = True
            data["final_status"] = "idle"
        return json.dumps({"event": "pane_agent_detected", "data": data})

    def test_agent_detected_records_agent_started_at(self):
        client = Client(self.fake.path)
        self.assertTrue(activity.track(client, self.store, self.event(), None, T0))
        self.assertEqual(self.store.load()["terminals"]["term1"]["agent_started_at"], "2026-09-24T12:00:00Z")

    def test_released_event_is_ignored(self):
        client = Client(self.fake.path)
        self.assertFalse(activity.track(client, self.store, self.event(released=True), None, T0))
        self.assertEqual(self.store.load(), {})

    def test_missing_agent_is_ignored(self):
        client = Client(self.fake.path)
        self.assertFalse(activity.track(client, self.store, self.event(include_agent=False), None, T0))
        self.assertEqual(self.store.load(), {})

    def test_no_terminal_id_is_ignored(self):
        self.fake.handlers["pane.get"] = lambda p: {"pane": {"pane_id": p["pane_id"]}}
        client = Client(self.fake.path)
        self.assertFalse(activity.track(client, self.store, self.event(), None, T0))
        self.assertEqual(self.store.load(), {})

    def test_env_event_alone_is_enough_to_recognize_the_event(self):
        # Only HERDR_PLUGIN_EVENT says so; the JSON payload's own event name
        # (deliberately wrong here) must not be the only signal consulted.
        client = Client(self.fake.path)
        ev = json.dumps({"event": "something_else", "data": {"pane_id": "w1:p1", "agent": "claude"}})
        self.assertTrue(activity.track(client, self.store, ev, None, T0, "pane.agent_detected"))
        self.assertIn("term1", self.store.load()["terminals"])

    def test_data_type_alone_is_enough_to_recognize_the_event(self):
        # No "event" field at all and no env_event, only data.type.
        client = Client(self.fake.path)
        ev = json.dumps({"data": {"type": "pane_agent_detected", "pane_id": "w1:p1", "agent": "claude"}})
        self.assertTrue(activity.track(client, self.store, ev, None, T0))
        self.assertIn("term1", self.store.load()["terminals"])

    def test_also_writes_agent_started_at_onto_the_sessions_own_record(self):
        # When pane.get already reports the pane's agent_session, the start
        # is written directly onto that session's record too, so it is not
        # lost if the terminal_id later changes (e.g. a herdr restart).
        self.fake.handlers["pane.get"] = lambda p: {"pane": {
            "pane_id": p["pane_id"], "terminal_id": "term1",
            "agent_session": {"agent": "claude", "kind": "id", "value": "S1", "source": "herdr:claude"}}}
        client = Client(self.fake.path)
        self.assertTrue(activity.track(client, self.store, self.event(), None, T0))
        self.assertEqual(self.store.load()["claude:S1"]["agent_started_at"], "2026-09-24T12:00:00Z")

    def test_without_an_agent_session_only_the_terminal_is_recorded(self):
        client = Client(self.fake.path)
        self.assertTrue(activity.track(client, self.store, self.event(), None, T0))
        data = self.store.load()
        self.assertEqual(list(data.keys()), ["terminals"])

    def test_session_appearing_on_the_third_pane_get_gets_agent_started_at(self):
        # herdr usually only learns Claude's session id from its own startup
        # hook, shortly after the agent itself is detected; pane.get's first
        # replies here still carry no matching session.
        calls = {"n": 0}

        def pane_get(p):
            calls["n"] += 1
            pane = {"pane_id": p["pane_id"], "terminal_id": "term1"}
            if calls["n"] >= 3:
                pane["agent_session"] = {"agent": "claude", "kind": "id", "value": "S1", "source": "herdr:claude"}
            return {"pane": pane}

        self.fake.handlers["pane.get"] = pane_get
        client = Client(self.fake.path)
        self.assertTrue(activity.track(client, self.store, self.event(), None, T0))
        self.assertEqual(calls["n"], 3)
        data = self.store.load()
        self.assertEqual(data["claude:S1"]["agent_started_at"], "2026-09-24T12:00:00Z")
        self.assertEqual(data["terminals"]["term1"]["agent_started_at"], "2026-09-24T12:00:00Z")

    def test_session_never_appearing_only_records_the_terminal_after_retrying(self):
        calls = {"n": 0}

        def pane_get(p):
            calls["n"] += 1
            return {"pane": {"pane_id": p["pane_id"], "terminal_id": "term1"}}

        self.fake.handlers["pane.get"] = pane_get
        client = Client(self.fake.path)
        self.assertTrue(activity.track(client, self.store, self.event(), None, T0))
        self.assertEqual(calls["n"], 1 + activity.AGENT_SESSION_RETRY_ATTEMPTS)
        data = self.store.load()
        self.assertEqual(list(data.keys()), ["terminals"])

    def test_stale_session_of_a_different_agent_is_never_marked_started(self):
        # pane.get keeps reporting a codex session throughout, even though
        # data["agent"] says claude was just (re)detected: the mismatched
        # session must never get agent_started_at, not even after retries
        # are exhausted.
        self.fake.handlers["pane.get"] = lambda p: {"pane": {
            "pane_id": p["pane_id"], "terminal_id": "term1",
            "agent_session": {"agent": "codex", "kind": "id", "value": "OTHER", "source": "herdr:codex"}}}
        client = Client(self.fake.path)
        self.assertTrue(activity.track(client, self.store, self.event(agent="claude"), None, T0))
        data = self.store.load()
        self.assertNotIn("codex:OTHER", data)
        self.assertEqual(list(data.keys()), ["terminals"])


if __name__ == "__main__":
    unittest.main()
