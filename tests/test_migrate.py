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

    def test_archive_dir_with_only_a_stray_file_does_not_count(self):
        # A stray file (e.g. macOS's .DS_Store) is not an archive entry and
        # must not be mistaken for legacy state left to migrate.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "archive").mkdir(parents=True)
            (Path(d) / "archive" / ".DS_Store").write_text("")
            self.assertFalse(migrate.root_legacy_present(Path(d)))

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

    def test_colliding_archive_id_with_different_content_moves_root_copy_to_conflict_dir(self):
        self.write_archive(self.default, "20260101T000000Z-aaaaaa", "already-here")
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")

        with self.assertLogs("shelf", level="WARNING") as cm:
            migrate.merge_into_default_session(self.root)

        self.assertTrue(any("20260101T000000Z-aaaaaa" in m for m in cm.output))
        # Neither copy was overwritten; the root copy moved aside rather
        # than staying in archive/ (which would warn again every run).
        self.assertFalse((self.root / "archive" / "20260101T000000Z-aaaaaa").exists())
        dest = json.loads((self.default / "archive" / "20260101T000000Z-aaaaaa" / "record.json").read_text())
        conflict = json.loads(
            (self.root / "archive.conflict" / "20260101T000000Z-aaaaaa" / "record.json").read_text())
        self.assertEqual(dest["tab"]["label"], "already-here")
        self.assertEqual(conflict["tab"]["label"], "old")

    def test_colliding_archive_id_with_identical_content_deletes_the_root_copy(self):
        self.write_archive(self.default, "20260101T000000Z-aaaaaa", "same-everywhere")
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "same-everywhere")

        handler = _CapturingHandler()
        log = logging.getLogger("shelf")
        log.addHandler(handler)
        try:
            migrate.merge_into_default_session(self.root)
        finally:
            log.removeHandler(handler)

        self.assertFalse(any(r.levelno >= logging.WARNING for r in handler.records), handler.records)
        self.assertFalse((self.root / "archive" / "20260101T000000Z-aaaaaa").exists())
        self.assertFalse((self.root / "archive.conflict").exists())
        dest = json.loads((self.default / "archive" / "20260101T000000Z-aaaaaa" / "record.json").read_text())
        self.assertEqual(dest["tab"]["label"], "same-everywhere")

    def test_colliding_archive_id_identity_check_covers_the_sessions_subtree(self):
        # record.json alone matching is not enough: a copied Claude session
        # file that differs must still count as "different".
        self.write_archive(self.default, "20260101T000000Z-aaaaaa", "same-record")
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "same-record")
        (self.default / "archive" / "20260101T000000Z-aaaaaa" / "sessions").mkdir()
        (self.default / "archive" / "20260101T000000Z-aaaaaa" / "sessions" / "S1.jsonl").write_text("dest-copy\n")
        (self.root / "archive" / "20260101T000000Z-aaaaaa" / "sessions").mkdir()
        (self.root / "archive" / "20260101T000000Z-aaaaaa" / "sessions" / "S1.jsonl").write_text("root-copy\n")

        migrate.merge_into_default_session(self.root)

        self.assertFalse((self.root / "archive" / "20260101T000000Z-aaaaaa").exists())
        self.assertTrue((self.root / "archive.conflict" / "20260101T000000Z-aaaaaa").exists())

    def test_archive_conflict_does_not_warn_again_on_a_second_run(self):
        self.write_archive(self.default, "20260101T000000Z-aaaaaa", "already-here")
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")
        with self.assertLogs("shelf", level="WARNING"):
            migrate.merge_into_default_session(self.root)

        handler = _CapturingHandler()
        log = logging.getLogger("shelf")
        log.addHandler(handler)
        try:
            migrate.merge_into_default_session(self.root)
        finally:
            log.removeHandler(handler)
        self.assertEqual(handler.records, [])
        # The conflict copy from the first run is untouched.
        self.assertTrue((self.root / "archive.conflict" / "20260101T000000Z-aaaaaa").exists())

    def test_non_colliding_ids_still_migrate_alongside_a_collision(self):
        self.write_archive(self.default, "20260101T000000Z-aaaaaa", "already-here")
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")
        self.write_archive(self.root, "20260102T000000Z-bbbbbb", "also-old")

        with self.assertLogs("shelf", level="WARNING"):
            migrate.merge_into_default_session(self.root)

        self.assertTrue((self.default / "archive" / "20260102T000000Z-bbbbbb" / "record.json").exists())
        self.assertTrue((self.root / "archive.conflict" / "20260101T000000Z-aaaaaa" / "record.json").exists())
        self.assertFalse((self.root / "archive" / "20260102T000000Z-bbbbbb").exists())
        self.assertFalse((self.root / "archive" / "20260101T000000Z-aaaaaa").exists())

    # -- Stray, non-archive entries in root archive/ (e.g. macOS's
    # .DS_Store) are left alone entirely: not migrated, not warned about,
    # and not counted as remaining legacy state. --

    def test_stray_file_in_root_archive_is_left_untouched(self):
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")
        (self.root / "archive" / ".DS_Store").write_text("junk")

        handler = _CapturingHandler()
        log = logging.getLogger("shelf")
        log.addHandler(handler)
        try:
            migrate.merge_into_default_session(self.root)
        finally:
            log.removeHandler(handler)

        # The real archive still migrates and logs its own info line; only
        # a warning or error about the stray file would be a problem.
        self.assertFalse(any(r.levelno >= logging.WARNING for r in handler.records), handler.records)
        self.assertTrue((self.default / "archive" / "20260101T000000Z-aaaaaa" / "record.json").exists())
        self.assertFalse((self.root / "archive" / "20260101T000000Z-aaaaaa").exists())
        self.assertEqual((self.root / "archive" / ".DS_Store").read_text(), "junk")

    def test_stray_file_also_in_destination_is_ignored(self):
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")
        (self.root / "archive" / ".DS_Store").write_text("root-junk")
        (self.default / "archive").mkdir(parents=True)
        (self.default / "archive" / ".DS_Store").write_text("dest-junk")

        handler = _CapturingHandler()
        log = logging.getLogger("shelf")
        log.addHandler(handler)
        try:
            migrate.merge_into_default_session(self.root)
        finally:
            log.removeHandler(handler)

        self.assertFalse(any(r.levelno >= logging.WARNING for r in handler.records), handler.records)
        self.assertTrue((self.default / "archive" / "20260101T000000Z-aaaaaa" / "record.json").exists())
        # Neither stray file was touched -- no collision handling was even
        # attempted for a non-archive entry.
        self.assertEqual((self.root / "archive" / ".DS_Store").read_text(), "root-junk")
        self.assertEqual((self.default / "archive" / ".DS_Store").read_text(), "dest-junk")

    def test_stray_directory_with_a_non_matching_name_is_left_untouched(self):
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")
        (self.root / "archive" / "not-an-archive-id").mkdir()
        (self.root / "archive" / "not-an-archive-id" / "note.txt").write_text("hello")

        handler = _CapturingHandler()
        log = logging.getLogger("shelf")
        log.addHandler(handler)
        try:
            migrate.merge_into_default_session(self.root)
        finally:
            log.removeHandler(handler)

        self.assertFalse(any(r.levelno >= logging.WARNING for r in handler.records), handler.records)
        self.assertTrue((self.root / "archive" / "not-an-archive-id" / "note.txt").exists())

    def test_second_run_with_only_a_stray_file_left_logs_nothing(self):
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")
        (self.root / "archive" / ".DS_Store").write_text("junk")
        migrate.merge_into_default_session(self.root)  # first run: migrates the real archive

        handler = _CapturingHandler()
        log = logging.getLogger("shelf")
        log.addHandler(handler)
        try:
            migrate.merge_into_default_session(self.root)  # second run
        finally:
            log.removeHandler(handler)

        self.assertEqual(handler.records, [])
        self.assertEqual((self.root / "archive" / ".DS_Store").read_text(), "junk")

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
        # A collision is now always resolved (moved aside or deleted -- see
        # the archive.conflict tests), so it can no longer be used to leave
        # something at the root; simulate an unresolvable failure instead.
        self.write_archive(self.root, "20260101T000000Z-aaaaaa", "old")
        with mock.patch("shelf.migrate._merge_archive", return_value=False):
            with self.assertLogs("shelf", level="ERROR") as cm:
                migrate.merge_into_default_session(self.root)
        self.assertTrue(any("could not" in m.lower() or "not migrated" in m.lower() for m in cm.output))
        # The command that triggered this must be able to continue: nothing
        # about the failure raises.
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

    def test_merge_takes_the_destination_activity_lock(self):
        # A concurrent activity.ActivityStore.update() for this session
        # (e.g. from a track hook) must be excluded while the merge reads,
        # merges and rewrites sessions/default/activity.json.
        _write(self.root / "activity.json", "{}")
        self.default.mkdir(parents=True)
        atomic_write_json(self.default / "activity.json", {"claude:S": {"first_seen": "2026-09-01T00:00:00Z"}})
        seen_locked = {}

        real_merge = migrate._merge_activity_data

        def check_lock_held(dest_data, src_data):
            try:
                with FileLock(self.default / "activity.lock", wait_seconds=0):
                    seen_locked["activity.lock"] = False
            except Exception:
                seen_locked["activity.lock"] = True
            return real_merge(dest_data, src_data)

        with mock.patch("shelf.migrate._merge_activity_data", side_effect=check_lock_held):
            migrate.merge_into_default_session(self.root)
        self.assertEqual(seen_locked, {"activity.lock": True})

    def test_concurrent_activity_store_update_during_merge_loses_nothing(self):
        # A real race, both threads released at once by a barrier: one
        # merges root activity.json into sessions/default/, the other
        # concurrently records new activity for a different session via
        # the normal ActivityStore.update() path. Whichever actually runs
        # first, activity.lock (see test above) serializes the two, so
        # neither side's write is lost.
        from shelf import activity

        _write(self.root / "activity.json", json.dumps({
            "claude:S": {"first_seen": "2026-09-01T00:00:00Z", "last_active": "2026-09-10T00:00:00Z"},
        }))
        self.default.mkdir(parents=True)
        atomic_write_json(self.default / "activity.json", {
            "claude:S": {"first_seen": "2026-09-01T00:00:00Z"},
        })
        store = activity.ActivityStore(self.default)
        errors = []
        ready = threading.Barrier(2)

        def do_merge():
            ready.wait(5)
            try:
                migrate.merge_into_default_session(self.root)
            except Exception as e:  # pragma: no cover - surfaced via errors
                errors.append(e)

        def do_update():
            ready.wait(5)

            def apply(d):
                d.setdefault("claude:other", {})["first_seen"] = "2026-09-20T00:00:00Z"
                return True
            try:
                store.update(apply)
            except Exception as e:  # pragma: no cover - surfaced via errors
                errors.append(e)

        t1 = threading.Thread(target=do_merge)
        t2 = threading.Thread(target=do_update)
        t1.start()
        t2.start()
        t1.join(10)
        t2.join(10)

        self.assertEqual(errors, [])
        final = read_json(self.default / "activity.json", None)
        self.assertIn("claude:S", final)
        self.assertEqual(final["claude:S"]["last_active"], "2026-09-10T00:00:00Z")
        self.assertIn("claude:other", final)
        self.assertEqual(final["claude:other"]["first_seen"], "2026-09-20T00:00:00Z")

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
