import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from shelf.__main__ import main


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HERDR_SOCKET_PATH", None)
        os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)

    def test_hooks_exit_zero_without_herdr(self):
        for argv in (["track"], ["sweep", "--if-due"], ["open-picker"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                self.assertEqual(main(argv), 0)

    def test_hook_swallows_unexpected_errors(self):
        with mock.patch("shelf.__main__.activity.track", side_effect=RuntimeError("boom")), \
                mock.patch("shelf.__main__.Client"), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["track"]), 0)

    def test_unknown_command(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["frobnicate"]), 2)

    def test_archive_and_restore_need_an_argument(self):
        for argv in (["archive"], ["restore"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                self.assertEqual(main(argv), 2)

    def test_list_empty(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["list"]), 0)
        self.assertIn("No archived tabs.", out.getvalue())


if __name__ == "__main__":
    unittest.main()
