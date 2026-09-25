import io
import json
import logging
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from shelf.__main__ import main
from tests.fakeherdr import FakeError, FakeHerdr


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HERDR_SOCKET_PATH", None)
        os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
        # main() attaches a delayed FileHandler to the shared "shelf" logger
        # pointed at this test's temp state dir; detach it before the temp
        # dir is removed so later tests (in this or other modules) sharing
        # the same logger name do not try to write to a deleted directory.
        self.addCleanup(self._reset_shelf_logger)

    @staticmethod
    def _reset_shelf_logger():
        shelf_log = logging.getLogger("shelf")
        for handler in shelf_log.handlers:
            handler.close()
        shelf_log.handlers = []

    def test_hooks_exit_zero_without_herdr(self):
        for argv in (["track"], ["sweep", "--if-due"], ["open-picker"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                self.assertEqual(main(argv), 0)

    def test_hook_swallows_unexpected_errors(self):
        with mock.patch("shelf.__main__.activity.track", side_effect=RuntimeError("boom")), \
                mock.patch("shelf.__main__.Client"), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["track"]), 0)

    def test_hook_writes_to_shelf_log(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["track"]), 0)
        log_path = Path(self.tmp.name) / "shelf.log"
        self.assertTrue(log_path.exists())
        self.assertIn("track", log_path.read_text())

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

    def test_list_shows_one_record_without_a_picker_number(self):
        archive_dir = Path(self.tmp.name) / "archive" / "20260101T000000Z-abcdef"
        archive_dir.mkdir(parents=True)
        (archive_dir / "record.json").write_text(json.dumps({
            "id": "20260101T000000Z-abcdef", "archived_at": "2026-01-01T00:00:00Z",
            "tab": {"label": "demo"}, "workspace": {"label": "ws"},
            "panes": {"p": {"agent": "claude", "last_activity": "2026-01-01T00:00:00Z"}},
        }))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["list"]), 0)
        self.assertIn("20260101T000000Z-abcdef  demo", out.getvalue())

    def test_list_ignores_a_broken_config(self):
        config_dir = os.path.join(self.tmp.name, "config")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.json"), "w") as f:
            f.write("{not valid json")
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": config_dir}):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["list"]), 0)
            self.assertIn("No archived tabs.", out.getvalue())

    def test_restore_unknown_id_exits_with_status_one(self):
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path}):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["restore", "20260101T000000Z-abcdef"])
            self.assertEqual(code, 1)
            self.assertIn("no archived tab '20260101T000000Z-abcdef'", err.getvalue())
            self.assertIn("python3 -m shelf list", err.getvalue())

    def test_state_dir_defaults_to_the_herdr_style_path(self):
        os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
        home = os.path.join(self.tmp.name, "home")
        xdg_state = os.path.join(self.tmp.name, "xdg-state")
        os.makedirs(home, exist_ok=True)
        with mock.patch.dict(os.environ, {"HOME": home, "XDG_STATE_HOME": xdg_state}):
            state_dir = Path(xdg_state) / "herdr" / "plugins" / "anilkmr.shelf"
            archive_dir = state_dir / "archive" / "20260101T000000Z-abcdef"
            archive_dir.mkdir(parents=True)
            (archive_dir / "record.json").write_text(json.dumps({
                "id": "20260101T000000Z-abcdef", "archived_at": "2026-01-01T00:00:00Z",
                "tab": {"label": "demo"}, "workspace": {"label": None}, "panes": {},
            }))
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["list"]), 0)
            self.assertIn("20260101T000000Z-abcdef", out.getvalue())

    def test_config_dir_defaults_to_the_herdr_style_path(self):
        os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
        home = os.path.join(self.tmp.name, "home")
        xdg_config = os.path.join(self.tmp.name, "xdg-config")
        os.makedirs(home, exist_ok=True)
        with mock.patch.dict(os.environ, {"HOME": home, "XDG_CONFIG_HOME": xdg_config}):
            config_dir = Path(xdg_config) / "herdr" / "plugins" / "config" / "anilkmr.shelf"
            config_dir.mkdir(parents=True)
            (config_dir / "config.json").write_text('{"idle_days": -1}')
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(main(["sweep", "--if-due"]), 0)
            self.assertIn("idle_days must be a positive number", err.getvalue())

    def test_manual_sweep_is_not_a_hook_and_reports_config_errors(self):
        config_dir = os.path.join(self.tmp.name, "config")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.json"), "w") as f:
            f.write('{"idle_days": -1}')
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": config_dir}), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["sweep"]), 1)

    def test_manual_sweep_notifies_when_nothing_to_archive(self):
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        fake.handlers.update({
            "tab.list": lambda p: {"tabs": []},
            "pane.list": lambda p: {"panes": []},
            "notification.show": lambda p: {"type": "ok"},
        })
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path}), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sweep"]), 0)
        self.assertIn(("notification.show", {"title": "shelf", "body": "shelf: nothing to archive"}), fake.calls)

    def test_config_error_notification_is_rate_limited(self):
        config_dir = os.path.join(self.tmp.name, "config")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.json"), "w") as f:
            f.write('{"idle_days": -1}')
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": config_dir}):
            with mock.patch("shelf.__main__.Client") as client_cls, redirect_stderr(io.StringIO()):
                client = client_cls.return_value
                self.assertEqual(main(["sweep", "--if-due"]), 0)
                self.assertEqual(main(["sweep", "--if-due"]), 0)
                calls = [c for c in client.call.call_args_list if c.args[0] == "notification.show"]
                self.assertEqual(len(calls), 1)
                marker = Path(self.tmp.name) / "config-error-notified"
                self.assertTrue(marker.exists())
                old = time.time() - 3700
                os.utime(marker, (old, old))
                self.assertEqual(main(["sweep", "--if-due"]), 0)
                calls = [c for c in client.call.call_args_list if c.args[0] == "notification.show"]
                self.assertEqual(len(calls), 2)

    def test_open_picker_sends_the_plugin_pane_open_payload(self):
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        fake.handlers["plugin.pane.open"] = lambda p: {"type": "ok"}
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path, "HERDR_PLUGIN_ID": "anilkmr.shelf"}):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(["open-picker"]), 0)
        self.assertEqual(fake.calls, [("plugin.pane.open", {"plugin_id": "anilkmr.shelf", "entrypoint": "picker"})])

    def test_open_picker_notifies_when_popup_is_busy(self):
        fake = FakeHerdr()
        self.addCleanup(fake.close)

        def open_pane(_params):
            raise FakeError("ui_busy", "busy")

        fake.handlers["plugin.pane.open"] = open_pane
        fake.handlers["notification.show"] = lambda p: {"type": "ok"}
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path}):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(["open-picker"]), 0)
        self.assertIn(("notification.show", {"title": "shelf", "body": "shelf: close the open popup first"}),
                      fake.calls)


if __name__ == "__main__":
    unittest.main()
