import json
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from shelf import migrate
from shelf.util import FileLock, atomic_write_json, read_json


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class RootLegacyPresentTest(unittest.TestCase):
    def test_nothing_present(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(migrate.root_legacy_present(Path(d)))

    def test_activity_json_present(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d) / "activity.json", "{}")
            self.assertTrue(migrate.root_legacy_present(Path(d)))

    def test_last_sweep_present(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d) / "last_sweep", "2026-01-01T00:00:00Z\n")
            self.assertTrue(migrate.root_legacy_present(Path(d)))

    def test_installed_at_present(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d) / "installed_at", "2026-01-01T00:00:00Z\n")
            self.assertTrue(migrate.root_legacy_present(Path(d)))

    def test_empty_archive_dir_does_not_count(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "archive").mkdir(parents=True)
            self.assertFalse(migrate.root_legacy_present(Path(d)))

    def test_non_empty_archive_dir_counts(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "archive" / "20260101T000000Z-aaaaaa").mkdir(parents=True)
            self.assertTrue(migrate.root_legacy_present(Path(d)))

    def test_unrelated_sessions_dir_does_not_count(self):
        # A subdirectory for some other (possibly disabled) herdr session
        # existing already must not be mistaken for root-level legacy state.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "sessions" / "cao").mkdir(parents=True)
            self.assertFalse(migrate.root_legacy_present(Path(d)))


class MergeIntoDefaultSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.default = self.root / "sessions" / "default"

    def write_archive(self, base: Path, archive_id: str, label: str) -> None:
        d = base / "archive" / archive_id
        d.mkdir(parents=True)
        (d / "record.json").write_text(json.dumps({
            "id": archive_id, "archived_at": "2026-01-01T00:00:00Z",
            "tab": {"label": label}, "workspace": {"label": None}, "panes": {},
        }))

    def test_fresh_install_creates_nothing(self):
        migrate.merge_into_default_session(self.root)
        self.assertFalse((self.root / "sessions").exists())
        self.assertFalse((self.root / "migrate.lock").exists())

    def test_disabled_only_session_present_creates_nothing_at_the_root(self):
        (self.root / "sessions" / "cao").mkdir(parents=True)
        migrate.merge_into_default_session(self.root)
        self.assertFalse((self.root / "migrate.lock").exists())
        self.assertFalse((self.root / "sessions" / "default").exists())

    def test_moves_all_legacy_files(self):
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")
        _write(self.root / "activity.json", json.dumps({"claude:S": {"first_seen": "2026-09-01T00:00:00Z"}}))
        _write(self.root / "last_sweep", "2026-09-01T00:00:00Z\n")
        _write(self.root / "installed_at", "2026-08-01T00:00:00Z\n")

        migrate.merge_into_default_session(self.root)

        self.assertTrue((self.default / "archive" / "20260101T000000Z-aaaaaa" / "record.json").exists())
        self.assertEqual(read_json(self.default / "activity.json", None)["claude:S"]["first_seen"],
                         "2026-09-01T00:00:00Z")
        self.assertEqual((self.default / "last_sweep").read_text(), "2026-09-01T00:00:00Z\n")
        self.assertEqual((self.default / "installed_at").read_text(), "2026-08-01T00:00:00Z\n")
        self.assertFalse((self.root / "activity.json").exists())
        self.assertFalse((self.root / "archive").exists())
        self.assertFalse((self.root / "last_sweep").exists())
        self.assertFalse((self.root / "installed_at").exists())

    def test_empty_sessions_dir_present_still_migrates_root_archives(self):
        (self.root / "sessions").mkdir(parents=True)  # e.g. left by a prior no-op run
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")

        migrate.merge_into_default_session(self.root)

        self.assertTrue((self.default / "archive" / "20260101T000000Z-aaaaaa" / "record.json").exists())
        self.assertFalse((self.root / "archive").exists())

    def test_partial_migration_finishes_archives_on_a_later_run(self):
        # activity.json already migrated; archive/ still sitting at the root
        # (e.g. the process was killed between the two).
        self.default.mkdir(parents=True)
        _write(self.default / "activity.json", "{}")
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")

        migrate.merge_into_default_session(self.root)

        self.assertTrue((self.default / "archive" / "20260101T000000Z-aaaaaa" / "record.json").exists())
        self.assertFalse((self.root / "archive").exists())

    def test_colliding_archive_id_is_left_at_the_root_with_a_warning(self):
        self.write_archive(self.default, "20260101T000000Z-aaaaaa", "already-here")
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")

        with self.assertLogs("shelf", level="WARNING") as cm:
            migrate.merge_into_default_session(self.root)

        self.assertTrue(any("20260101T000000Z-aaaaaa" in m for m in cm.output))
        # Neither copy was overwritten.
        dest = json.loads((self.default / "archive" / "20260101T000000Z-aaaaaa" / "record.json").read_text())
        src = json.loads((self.root / "archive" / "20260101T000000Z-aaaaaa" / "record.json").read_text())
        self.assertEqual(dest["tab"]["label"], "already-here")
        self.assertEqual(src["tab"]["label"], "old")

    def test_non_colliding_ids_still_migrate_alongside_a_collision(self):
        self.write_archive(self.default, "20260101T000000Z-aaaaaa", "already-here")
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")
        self.write_archive(self.root, "20260102T000000Z-bbbbbb", "also-old")

        with self.assertLogs("shelf", level="WARNING"):
            migrate.merge_into_default_session(self.root)

        self.assertTrue((self.default / "archive" / "20260102T000000Z-bbbbbb" / "record.json").exists())
        self.assertTrue((self.root / "archive" / "20260101T000000Z-aaaaaa" / "record.json").exists())
        self.assertFalse((self.root / "archive" / "20260102T000000Z-bbbbbb").exists())

    def test_rollback_scenario_migrates_new_archives_on_a_later_run(self):
        # First upgrade: migrate one archive.
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")
        migrate.merge_into_default_session(self.root)
        self.assertTrue((self.default / "archive" / "20260101T000000Z-aaaaaa").exists())
        self.assertFalse((self.root / "archive").exists())

        # Rollback to 0.2.x, which only knows the root-level layout, and
        # archives a second tab there.
        self.write_archive(self.root, "20260102T000000Z-bbbbbb", "also-old")

        # Upgrade again: the second run must pick up the new one.
        migrate.merge_into_default_session(self.root)
        self.assertTrue((self.default / "archive" / "20260102T000000Z-bbbbbb").exists())
        self.assertFalse((self.root / "archive").exists())

    def test_running_again_after_a_full_migration_is_a_noop(self):
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")
        _write(self.root / "activity.json", "{}")
        migrate.merge_into_default_session(self.root)
        handler = _CapturingHandler()
        log = logging.getLogger("shelf")
        log.addHandler(handler)
        try:
            migrate.merge_into_default_session(self.root)
        finally:
            log.removeHandler(handler)
        self.assertEqual(handler.records, [])

    def test_error_logged_when_something_remains_after_the_attempt(self):
        self.write_archive(self.default, "20260101T000000Z-aaaaaa", "already-here")
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")
        with self.assertLogs("shelf", level="ERROR") as cm:
            migrate.merge_into_default_session(self.root)
        self.assertTrue(any("could not" in m.lower() or "not migrated" in m.lower() for m in cm.output))
        # The command that triggered this must be able to continue: nothing
        # about the failed collision raises.
        self.assertTrue((self.root / "archive" / "20260101T000000Z-aaaaaa").exists())

    # -- activity.json merge semantics --

    def test_activity_json_merges_session_records_keeping_later_values(self):
        _write(self.root / "activity.json", json.dumps({
            "claude:S": {"first_seen": "2026-09-01T00:00:00Z", "last_active": "2026-09-10T00:00:00Z"},
        }))
        self.default.mkdir(parents=True)
        atomic_write_json(self.default / "activity.json", {
            "claude:S": {"first_seen": "2026-09-01T00:00:00Z", "last_active": "2026-09-05T00:00:00Z",
                        "restored_at": "2026-09-12T00:00:00Z"},
        })

        migrate.merge_into_default_session(self.root)

        merged = read_json(self.default / "activity.json", None)["claude:S"]
        self.assertEqual(merged["last_active"], "2026-09-10T00:00:00Z")  # root's is later
        self.assertEqual(merged["restored_at"], "2026-09-12T00:00:00Z")  # only in destination
        self.assertFalse((self.root / "activity.json").exists())

    def test_activity_json_merge_unions_terminals(self):
        _write(self.root / "activity.json", json.dumps({
            "terminals": {"term_a": {"agent_started_at": "2026-09-01T00:00:00Z"}},
        }))
        self.default.mkdir(parents=True)
        atomic_write_json(self.default / "activity.json", {
            "terminals": {"term_b": {"agent_started_at": "2026-09-02T00:00:00Z"}},
        })

        migrate.merge_into_default_session(self.root)

        terminals = read_json(self.default / "activity.json", None)["terminals"]
        self.assertEqual(set(terminals), {"term_a", "term_b"})

    def test_activity_json_only_at_destination_is_untouched(self):
        self.default.mkdir(parents=True)
        atomic_write_json(self.default / "activity.json", {"claude:S": {"first_seen": "2026-09-01T00:00:00Z"}})
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")  # something else to migrate

        migrate.merge_into_default_session(self.root)

        self.assertEqual(read_json(self.default / "activity.json", None),
                         {"claude:S": {"first_seen": "2026-09-01T00:00:00Z"}})

    # -- last_sweep / installed_at semantics --

    def test_last_sweep_keeps_the_destination_when_both_exist(self):
        _write(self.root / "last_sweep", "2026-09-05T00:00:00Z\n")
        self.default.mkdir(parents=True)
        _write(self.default / "last_sweep", "2026-09-10T00:00:00Z\n")

        migrate.merge_into_default_session(self.root)

        self.assertEqual((self.default / "last_sweep").read_text(), "2026-09-10T00:00:00Z\n")
        self.assertFalse((self.root / "last_sweep").exists())

    def test_installed_at_keeps_the_earlier_of_the_two(self):
        _write(self.root / "installed_at", "2026-08-01T00:00:00Z\n")
        self.default.mkdir(parents=True)
        _write(self.default / "installed_at", "2026-09-01T00:00:00Z\n")

        migrate.merge_into_default_session(self.root)

        self.assertEqual((self.default / "installed_at").read_text(), "2026-08-01T00:00:00Z\n")
        self.assertFalse((self.root / "installed_at").exists())

    def test_installed_at_keeps_the_destination_when_it_is_already_earlier(self):
        _write(self.root / "installed_at", "2026-09-01T00:00:00Z\n")
        self.default.mkdir(parents=True)
        _write(self.default / "installed_at", "2026-08-01T00:00:00Z\n")

        migrate.merge_into_default_session(self.root)

        self.assertEqual((self.default / "installed_at").read_text(), "2026-08-01T00:00:00Z\n")

    # -- Locking / concurrency --

    def test_takes_root_sweep_and_activity_locks_while_merging(self):
        # Excludes a 0.2.x process mid-sweep or mid-track, which still uses
        # these locks at the root.
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")
        seen_locked = {}

        def check_locks_held(root, default_dir):
            for name in ("sweep.lock", "activity.lock", "migrate.lock"):
                try:
                    with FileLock(root / name, wait_seconds=0):
                        seen_locked[name] = False
                except Exception:
                    seen_locked[name] = True
            return False

        with mock.patch("shelf.migrate._merge_archive", side_effect=check_locks_held):
            migrate.merge_into_default_session(self.root)
        self.assertTrue(all(seen_locked.values()), seen_locked)

    def test_lock_busy_logs_a_warning_and_does_not_raise(self):
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")
        with FileLock(self.root / "sweep.lock"), \
                mock.patch("shelf.migrate.LOCK_WAIT_SECONDS", 0.1):
            with self.assertLogs("shelf", level="WARNING"):
                migrate.merge_into_default_session(self.root)  # must not raise
        # Nothing was migrated: the 0.2.x-lookalike lock holder excluded us.
        self.assertTrue((self.root / "archive" / "20260101T000000Z-aaaaaa").exists())

    def test_concurrent_migration_loses_nothing(self):
        for i in range(5):
            self.write_archive(self.root, f"2026010{i}T000000Z-aaaaa{i}", f"old-{i}")
        _write(self.root / "activity.json", json.dumps({"claude:S": {"first_seen": "2026-09-01T00:00:00Z"}}))

        errors = []

        def run():
            try:
                migrate.merge_into_default_session(self.root)
            except Exception as e:  # pragma: no cover - surfaced via errors
                errors.append(e)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        self.assertEqual(errors, [])
        for i in range(5):
            self.assertTrue((self.default / "archive" / f"2026010{i}T000000Z-aaaaa{i}").exists())
        self.assertFalse((self.root / "archive").exists())
        self.assertFalse((self.root / "activity.json").exists())
        self.assertEqual(read_json(self.default / "activity.json", None)["claude:S"]["first_seen"],
                         "2026-09-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
