import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from shelf import util


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

    def test_garbage(self):
        for bad in (None, "", "yesterday", 42):
            self.assertIsNone(util.parse_iso(bad))

    def test_iso_roundtrip(self):
        dt = datetime(2026, 9, 24, 10, 15, 7, tzinfo=timezone.utc)
        self.assertEqual(util.iso(dt), "2026-09-24T10:15:07Z")
        self.assertEqual(util.parse_iso(util.iso(dt)), dt)


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


class FileLockTest(unittest.TestCase):
    def test_second_holder_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.lock"
            with util.FileLock(p):
                with self.assertRaises(util.LockBusy):
                    with util.FileLock(p):
                        pass
            self.assertFalse(p.exists())

    def test_stale_lock_is_taken_over(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.lock"
            p.write_text("999999")
            old = time.time() - 3600
            os.utime(p, (old, old))
            with util.FileLock(p, stale_seconds=600):
                self.assertTrue(p.exists())
            self.assertFalse(p.exists())


if __name__ == "__main__":
    unittest.main()
