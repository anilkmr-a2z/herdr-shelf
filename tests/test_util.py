import errno
import multiprocessing
import os
import signal
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from shelf import util


def _hold_lock_until_killed(path, ready):
    with util.FileLock(Path(path)):
        ready.set()
        time.sleep(100)


class ParseIsoTest(unittest.TestCase):
    def test_z_suffix(self):
        self.assertEqual(util.parse_iso("2026-09-24T10:15:00Z"),
                         datetime(2026, 9, 24, 10, 15, tzinfo=timezone.utc))

    def test_millis(self):
        self.assertEqual(util.parse_iso("2026-08-01T05:07:50.770Z").microsecond, 770000)

    def test_seven_digit_fraction(self):
        self.assertEqual(util.parse_iso("2026-08-01T05:07:50.1234567+00:00").microsecond, 123456)

    def test_naive_is_utc(self):
        self.assertEqual(util.parse_iso("2026-08-01T05:07:50").tzinfo, timezone.utc)

    def test_offset_converted(self):
        self.assertEqual(util.parse_iso("2026-08-01T07:00:00+02:00").hour, 5)

    def test_offset_without_colon(self):
        self.assertEqual(util.parse_iso("2026-08-01T07:00:00+0200").hour, 5)

    def test_garbage(self):
        for bad in (None, "", "yesterday", 42):
            self.assertIsNone(util.parse_iso(bad))

    def test_iso_roundtrip(self):
        dt = datetime(2026, 9, 24, 10, 15, 7, tzinfo=timezone.utc)
        self.assertEqual(util.iso(dt), "2026-09-24T10:15:07Z")
        self.assertEqual(util.parse_iso(util.iso(dt)), dt)

    def test_iso_naive_is_utc(self):
        dt = datetime(2026, 9, 24, 10, 15, 7)
        self.assertEqual(util.iso(dt), "2026-09-24T10:15:07Z")


class JsonFileTest(unittest.TestCase):
    def test_write_then_read(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "x.json"
            util.atomic_write_json(p, {"a": 1})
            self.assertEqual(util.read_json(p, None), {"a": 1})
            self.assertEqual([f.name for f in p.parent.iterdir()], ["x.json"])

    def test_missing_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(util.read_json(Path(d) / "nope.json", {"d": 0}), {"d": 0})

    def test_empty_file_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "empty.json"
            p.write_text("")
            self.assertEqual(util.read_json(p, {"d": 0}), {"d": 0})

    def test_corrupt_file_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "corrupt.json"
            p.write_text("{not json")
            self.assertEqual(util.read_json(p, {"d": 0}), {"d": 0})

    def test_corrupt_file_logs_warning(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "corrupt.json"
            p.write_text("{not json")
            with self.assertLogs("shelf", level="WARNING") as ctx:
                self.assertEqual(util.read_json(p, {"d": 0}), {"d": 0})
            self.assertIn(str(p), ctx.output[0])

    def test_write_failure_leaves_old_file_and_no_temp(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            util.atomic_write_json(p, {"a": 1})
            with self.assertRaises(TypeError):
                util.atomic_write_json(p, {"a": object()})
            self.assertEqual(util.read_json(p, None), {"a": 1})
            self.assertEqual([f.name for f in p.parent.iterdir()], ["x.json"])


class FileLockTest(unittest.TestCase):
    def test_second_holder_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.lock"
            with util.FileLock(p):
                with self.assertRaises(util.LockBusy):
                    with util.FileLock(p):
                        pass

    def test_lock_held_by_killed_process_can_be_acquired(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.lock"
            ready = multiprocessing.Event()
            proc = multiprocessing.Process(target=_hold_lock_until_killed, args=(str(p), ready))
            proc.start()
            self.addCleanup(lambda: proc.is_alive() and proc.kill())
            self.assertTrue(ready.wait(5), "child never acquired the lock")
            os.kill(proc.pid, signal.SIGKILL)
            proc.join(5)
            with util.FileLock(p, wait_seconds=2):
                pass

    def test_wait_succeeds_once_released(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.lock"
            started = threading.Event()

            def hold_briefly():
                with util.FileLock(p):
                    started.set()
                    time.sleep(0.2)

            t = threading.Thread(target=hold_briefly)
            t.start()
            self.assertTrue(started.wait(5))
            start = time.monotonic()
            with util.FileLock(p, wait_seconds=2):
                pass
            self.assertGreater(time.monotonic() - start, 0.05)
            t.join(5)

    def test_other_oserror_closes_fd_before_raising(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.lock"
            real_open = os.open
            opened = []

            def spy_open(*args, **kwargs):
                fd = real_open(*args, **kwargs)
                opened.append(fd)
                return fd

            with mock.patch("shelf.util.os.open", side_effect=spy_open), \
                 mock.patch("shelf.util.fcntl.flock", side_effect=OSError(errno.EACCES, "denied")):
                with self.assertRaises(OSError):
                    with util.FileLock(p):
                        pass
            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError):
                os.fstat(opened[0])


if __name__ == "__main__":
    unittest.main()
