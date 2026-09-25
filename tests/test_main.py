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
from shelf.api import HerdrError
from shelf.util import FileLock, iso, now
from tests.fakeherdr import FakeError, FakeHerdr


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # XDG_CONFIG_HOME/XDG_STATE_HOME are pointed at fresh temp subdirectories
        # (not derived from the real HOME) so a test that forgets to set
        # HERDR_PLUGIN_CONFIG_DIR never falls through to the developer's own
        # ~/.config or ~/.local/state and reads a real herdr plugin config/state.
        env = mock.patch.dict(os.environ, {
            "HERDR_PLUGIN_STATE_DIR": self.tmp.name,
            "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "xdg-config"),
            "XDG_STATE_HOME": os.path.join(self.tmp.name, "xdg-state"),
        })
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

    def test_developer_home_config_is_never_read(self):
        # A config.json sitting under a "real-looking" ~/.config path (as it
        # would on a developer's own machine) must never be consulted: setUp
        # points XDG_CONFIG_HOME elsewhere, so HOME alone must not matter.
        fake_home = os.path.join(self.tmp.name, "developer-home")
        broken_config_dir = Path(fake_home) / ".config" / "herdr" / "plugins" / "config" / "shelf"
        broken_config_dir.mkdir(parents=True)
        (broken_config_dir / "config.json").write_text("{not valid json")
        os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": fake_home}), redirect_stderr(err):
            self.assertEqual(main(["sweep", "--if-due"]), 0)
        self.assertNotIn("Expecting property name", err.getvalue())

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

    def test_if_due_hook_skips_loading_config_when_not_due(self):
        # A sweep just happened (last_sweep is recent), so this is not due
        # per the default 60-minute interval: config.load must never even be
        # called, so its "unknown key(s)" warning cannot flood shelf.log on
        # every focus-change hook between actual sweeps.
        (Path(self.tmp.name) / "last_sweep").write_text(iso(now()) + "\n")
        with mock.patch("shelf.__main__.config.load") as load, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["sweep", "--if-due"]), 0)
        load.assert_not_called()

    def test_pick_failure_is_logged(self):
        # No HERDR_SOCKET_PATH is set, so Client() raises inside _pick;
        # that failure must be logged (with a traceback), not just printed.
        with mock.patch("builtins.input", side_effect=EOFError), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["pick"]), 0)
        log_path = Path(self.tmp.name) / "shelf.log"
        self.assertTrue(log_path.exists())
        self.assertIn("pick failed", log_path.read_text())

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

    def test_restore_other_keyerrors_are_not_mistaken_for_a_missing_entry(self):
        archive_dir = Path(self.tmp.name) / "archive" / "20260101T000000Z-abcdef"
        archive_dir.mkdir(parents=True)
        (archive_dir / "record.json").write_text(json.dumps({
            "id": "20260101T000000Z-abcdef", "archived_at": "2026-01-01T00:00:00Z",
            "tab": {"label": "demo"}, "workspace": {"label": None}, "panes": {},
        }))
        with mock.patch("shelf.__main__.restore.restore", side_effect=KeyError("weird")), \
                mock.patch("shelf.__main__.Client"), redirect_stderr(io.StringIO()) as err:
            code = main(["restore", "20260101T000000Z-abcdef"])
        self.assertEqual(code, 1)
        self.assertNotIn("no archived tab", err.getvalue())

    def test_state_dir_defaults_to_the_herdr_style_path(self):
        os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
        home = os.path.join(self.tmp.name, "home")
        xdg_state = os.path.join(self.tmp.name, "xdg-state")
        os.makedirs(home, exist_ok=True)
        with mock.patch.dict(os.environ, {"HOME": home, "XDG_STATE_HOME": xdg_state}):
            state_dir = Path(xdg_state) / "herdr" / "plugins" / "shelf"
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
            config_dir = Path(xdg_config) / "herdr" / "plugins" / "config" / "shelf"
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
            "workspace.list": lambda p: {"workspaces": []},
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

    def test_manual_sweep_config_error_also_notifies(self):
        config_dir = os.path.join(self.tmp.name, "config")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.json"), "w") as f:
            f.write('{"idle_days": -1}')
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": config_dir}):
            with mock.patch("shelf.__main__.Client") as client_cls, redirect_stderr(io.StringIO()):
                client = client_cls.return_value
                self.assertEqual(main(["sweep"]), 1)  # manual sweep is not a hook: exit 1
                calls = [c for c in client.call.call_args_list if c.args[0] == "notification.show"]
                self.assertEqual(len(calls), 1)
                self.assertIn("shelf: config.json is invalid", calls[0].args[1]["body"])

    def test_config_error_notification_skips_silently_when_lock_is_busy(self):
        config_dir = os.path.join(self.tmp.name, "config")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.json"), "w") as f:
            f.write('{"idle_days": -1}')
        with FileLock(Path(self.tmp.name) / "config-error.lock"):
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": config_dir}):
                with mock.patch("shelf.__main__.Client") as client_cls, redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["sweep", "--if-due"]), 0)
                    client_cls.return_value.call.assert_not_called()

    def test_config_error_marker_is_not_touched_when_notification_fails(self):
        config_dir = os.path.join(self.tmp.name, "config")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.json"), "w") as f:
            f.write('{"idle_days": -1}')
        marker = Path(self.tmp.name) / "config-error-notified"
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": config_dir}):
            with mock.patch("shelf.__main__.Client") as client_cls, redirect_stderr(io.StringIO()):
                client_cls.return_value.call.side_effect = HerdrError("unavailable", "no herdr")
                self.assertEqual(main(["sweep", "--if-due"]), 0)
                self.assertFalse(marker.exists())
                # notification still fails, but since the marker was never
                # touched, this is not rate-limited: it tries again.
                self.assertEqual(main(["sweep", "--if-due"]), 0)
                calls = [c for c in client_cls.return_value.call.call_args_list if c.args[0] == "notification.show"]
                self.assertEqual(len(calls), 2)

    def test_open_picker_sends_the_plugin_pane_open_payload(self):
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        fake.handlers["plugin.pane.open"] = lambda p: {"type": "ok"}
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path, "HERDR_PLUGIN_ID": "shelf"}):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(["open-picker"]), 0)
        self.assertEqual(fake.calls, [("plugin.pane.open", {"plugin_id": "shelf", "entrypoint": "picker"})])

    def test_manual_archive_lock_busy_prints_friendly_message_not_the_lock_path(self):
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path}), \
                mock.patch("shelf.sweep.ARCHIVE_NOW_LOCK_WAIT_SECONDS", 0.1):
            with FileLock(Path(self.tmp.name) / "sweep.lock"):
                err = io.StringIO()
                with redirect_stderr(err):
                    code = main(["archive", "w1:t1"])
            self.assertEqual(code, 1)
            self.assertIn("shelf: a sweep is running; try again in a moment", err.getvalue())
            self.assertNotIn("sweep.lock", err.getvalue())

    def test_restore_lock_busy_prints_friendly_message_not_the_lock_path(self):
        archive_dir = Path(self.tmp.name) / "archive" / "20260101T000000Z-abcdef"
        archive_dir.mkdir(parents=True)
        (archive_dir / "record.json").write_text(json.dumps({
            "id": "20260101T000000Z-abcdef", "archived_at": "2026-01-01T00:00:00Z",
            "tab": {"label": "demo"}, "workspace": {"label": None}, "panes": {},
        }))
        with mock.patch("shelf.restore.RESTORE_LOCK_WAIT_SECONDS", 0.1), \
                mock.patch("shelf.__main__.Client"):
            with FileLock(Path(self.tmp.name) / "sweep.lock"):
                err = io.StringIO()
                with redirect_stderr(err):
                    code = main(["restore", "20260101T000000Z-abcdef"])
            self.assertEqual(code, 1)
            self.assertIn("shelf: a sweep is running; try again in a moment", err.getvalue())
            self.assertNotIn("sweep.lock", err.getvalue())

    def test_manual_sweep_lock_busy_prints_another_sweep_is_running(self):
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path}):
            with FileLock(Path(self.tmp.name) / "sweep.lock"):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = main(["sweep"])
            self.assertEqual(code, 0)
            self.assertIn("shelf: another sweep is running", out.getvalue())

    def test_hook_sweep_lock_busy_prints_nothing(self):
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path}):
            with FileLock(Path(self.tmp.name) / "sweep.lock"):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = main(["sweep", "--if-due"])
            self.assertEqual(code, 0)
            self.assertEqual(out.getvalue(), "")

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
        self.assertIn(("notification.show",
                       {"title": "shelf", "body": "shelf: close the open popup or dialog first"}),
                      fake.calls)


if __name__ == "__main__":
    unittest.main()
