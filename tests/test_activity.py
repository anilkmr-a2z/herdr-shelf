import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

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
        return json.dumps({"event": "pane.agent_status_changed", "data": {"pane_id": "w1:p1", "agent_status": status}})

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
        ev = json.dumps({"event": "pane.agent_status_changed", "data": {"agent_status": "blocked"}})
        self.assertTrue(activity.track(Client(self.fake.path), self.store, ev, "w1:p9", T0))
        self.assertEqual(self.fake.calls, [("pane.get", {"pane_id": "w1:p9"})])

    def test_garbage_event_is_ignored(self):
        self.assertFalse(activity.track(Client(self.fake.path), self.store, "not json", None, T0))
        self.assertFalse(activity.track(Client(self.fake.path), self.store, None, None, T0))


if __name__ == "__main__":
    unittest.main()
