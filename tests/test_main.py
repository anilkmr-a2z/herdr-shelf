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
        # Per-session state (archive/, activity.json, last_sweep,
        # installed_at, sweep.lock) lives under sessions/<name>/; with no
        # HERDR_SOCKET_PATH set, that name is "default". shelf.log,
        # config-error-notified and config-error.lock stay at the root
        # (self.tmp.name).
        self.session = Path(self.tmp.name) / "sessions" / "default"

    @staticmethod
    def _reset_shelf_logger():
        shelf_log = logging.getLogger("shelf")
        for handler in shelf_log.handlers:
            handler.close()
        shelf_log.handlers = []
        shelf_log.filters = []

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

    def test_track_passes_herdr_plugin_event_through(self):
        # HERDR_PLUGIN_EVENT (the hook's own dot-form event name) must reach
        # activity.track as its env_event argument.
        with mock.patch("shelf.__main__.activity.track") as track, \
                mock.patch("shelf.__main__.Client"), \
                mock.patch.dict(os.environ, {"HERDR_PLUGIN_EVENT": "pane.agent_detected"}), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(main(["track"]), 0)
        self.assertEqual(track.call_args.args[-1], "pane.agent_detected")

    def test_if_due_hook_skips_loading_config_when_not_due(self):
        # A sweep just happened (last_sweep is recent), so this is not due
        # per the default 60-minute interval: config.load must never even be
        # called, so its "unknown key(s)" warning cannot flood shelf.log on
        # every focus-change hook between actual sweeps.
        (self.session / "last_sweep").parent.mkdir(parents=True, exist_ok=True)
        (self.session / "last_sweep").write_text(iso(now()) + "\n")
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
        archive_dir = self.session / "archive" / "20260101T000000Z-abcdef"
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
        archive_dir = self.session / "archive" / "20260101T000000Z-abcdef"
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
            archive_dir = state_dir / "sessions" / "default" / "archive" / "20260101T000000Z-abcdef"
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
            with FileLock(self.session / "sweep.lock"):
                err = io.StringIO()
                with redirect_stderr(err):
                    code = main(["archive", "w1:t1"])
            self.assertEqual(code, 1)
            self.assertIn("shelf: a sweep is running; try again in a moment", err.getvalue())
            self.assertNotIn("sweep.lock", err.getvalue())

    def test_restore_lock_busy_prints_friendly_message_not_the_lock_path(self):
        archive_dir = self.session / "archive" / "20260101T000000Z-abcdef"
        archive_dir.mkdir(parents=True)
        (archive_dir / "record.json").write_text(json.dumps({
            "id": "20260101T000000Z-abcdef", "archived_at": "2026-01-01T00:00:00Z",
            "tab": {"label": "demo"}, "workspace": {"label": None}, "panes": {},
        }))
        with mock.patch("shelf.restore.RESTORE_LOCK_WAIT_SECONDS", 0.1), \
                mock.patch("shelf.__main__.Client"):
            with FileLock(self.session / "sweep.lock"):
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
            with FileLock(self.session / "sweep.lock"):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = main(["sweep"])
            self.assertEqual(code, 0)
            self.assertIn("shelf: another sweep is running", out.getvalue())

    def test_hook_sweep_lock_busy_prints_nothing(self):
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path}):
            with FileLock(self.session / "sweep.lock"):
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


class SessionAllowlistTest(unittest.TestCase):
    """Shelf must only act in herdr sessions the user lists (default: only
    "default"), and keep separate state per herdr session."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "HERDR_PLUGIN_STATE_DIR": self.tmp.name,
            "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "xdg-config"),
            "XDG_STATE_HOME": os.path.join(self.tmp.name, "xdg-state"),
        })
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HERDR_SOCKET_PATH", None)
        os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
        self.addCleanup(MainTest._reset_shelf_logger)
        self.root = Path(self.tmp.name)
        self.default_session = self.root / "sessions" / "default"
        self.cao_session = self.root / "sessions" / "cao"

    def use_cao_socket(self):
        """A HERDR_SOCKET_PATH whose session name parses as "cao", with no
        real socket file -- fine for any path where Shelf must not even try
        to reach herdr (a disabled hook, or a disabled manual command)."""
        path = os.path.join(self.tmp.name, "herdr-config", "sessions", "cao", "herdr.sock")
        mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": path}).start()
        self.addCleanup(lambda: os.environ.pop("HERDR_SOCKET_PATH", None))
        return path

    def use_cao_socket_linked_to(self, fake):
        """Like use_cao_socket, but the path is a real, reachable socket (a
        symlink to fake's own socket file), for a check that must actually
        call herdr (e.g. open-picker's disabled notification)."""
        link = Path(self.tmp.name) / "herdr-config" / "sessions" / "cao" / "herdr.sock"
        link.parent.mkdir(parents=True)
        os.symlink(fake.path, link)
        mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": str(link)}).start()
        self.addCleanup(lambda: os.environ.pop("HERDR_SOCKET_PATH", None))
        return str(link)

    def write_config(self, obj):
        config_dir = os.path.join(self.tmp.name, "config")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.json"), "w") as f:
            json.dump(obj, f)
        mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": config_dir}).start()
        self.addCleanup(lambda: os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None))

    # -- Hooks: a disabled session returns 0 immediately and writes nothing. --

    def test_disabled_hook_track_writes_no_state(self):
        self.use_cao_socket()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["track"]), 0)
        self.assertFalse(self.cao_session.exists())

    def test_disabled_hook_sweep_if_due_writes_no_state(self):
        self.use_cao_socket()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["sweep", "--if-due"]), 0)
        self.assertFalse(self.cao_session.exists())

    def test_disabled_hook_startup_sweep_writes_no_state(self):
        # The startup hook runs the exact same command as the
        # workspace.focused hook: "sweep --if-due".
        self.use_cao_socket()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["sweep", "--if-due"]), 0)
        self.assertFalse((self.cao_session / "last_sweep").exists())

    def test_enabled_hook_sweep_if_due_is_unaffected(self):
        # The default session is enabled by default, so its own hooks are
        # unaffected by another session being disabled.
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        fake.handlers.update({"tab.list": lambda p: {"tabs": []}, "pane.list": lambda p: {"panes": []}})
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path}):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["sweep", "--if-due"]), 0)
        self.assertTrue((self.default_session / "last_sweep").exists())

    def test_disabled_open_picker_shows_a_notification_instead_of_opening(self):
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        fake.handlers["notification.show"] = lambda p: {"type": "ok"}
        self.use_cao_socket_linked_to(fake)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["open-picker"]), 0)
        self.assertNotIn("plugin.pane.open", fake.methods())
        self.assertIn(("notification.show", {"title": "shelf", "body": "shelf is not enabled for herdr session cao"}),
                      fake.calls)

    # -- Manual commands: a disabled session prints a message and exits 1. --

    def test_disabled_manual_sweep_prints_message_and_exits_one(self):
        self.use_cao_socket()
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = main(["sweep"])
        self.assertEqual(code, 1)
        self.assertIn("shelf is not enabled for herdr session 'cao'", err.getvalue())
        self.assertIn('add it to "sessions" in config.json', err.getvalue())

    def test_disabled_manual_archive_prints_message_and_exits_one(self):
        self.use_cao_socket()
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["archive", "w1:t1"])
        self.assertEqual(code, 1)
        self.assertIn("shelf is not enabled for herdr session 'cao'", err.getvalue())

    def test_disabled_list_prints_message_and_exits_one(self):
        self.use_cao_socket()
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["list"])
        self.assertEqual(code, 1)
        self.assertIn("shelf is not enabled for herdr session 'cao'", err.getvalue())

    def test_disabled_restore_prints_message_and_exits_one(self):
        self.use_cao_socket()
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["restore", "20260101T000000Z-abcdef"])
        self.assertEqual(code, 1)
        self.assertIn("shelf is not enabled for herdr session 'cao'", err.getvalue())

    def test_disabled_pick_shows_message_in_the_popup_and_waits_for_enter(self):
        self.use_cao_socket()
        err = io.StringIO()
        with mock.patch("builtins.input", return_value="") as input_mock, redirect_stderr(err):
            code = main(["pick"])
        self.assertEqual(code, 0)
        self.assertIn("shelf is not enabled for herdr session 'cao'", err.getvalue())
        input_mock.assert_called_once_with("Press Enter to close. ")

    def test_list_with_no_socket_path_uses_the_default_session(self):
        # Configuring only "cao" leaves "default" (used when
        # HERDR_SOCKET_PATH is unset) disabled.
        self.write_config({"sessions": ["cao"]})
        os.environ.pop("HERDR_SOCKET_PATH", None)
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["list"])
        self.assertEqual(code, 1)
        self.assertIn("shelf is not enabled for herdr session 'default'", err.getvalue())

    # -- A named session, once allowed, works exactly like "default". --

    def test_named_session_can_be_allowed_and_gets_its_own_state(self):
        self.write_config({"sessions": ["default", "cao"], "mode": "live"})
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        fake.handlers.update({"tab.list": lambda p: {"tabs": []}, "pane.list": lambda p: {"panes": []}})
        self.use_cao_socket_linked_to(fake)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["sweep", "--if-due"]), 0)
        self.assertTrue((self.cao_session / "last_sweep").exists())
        self.assertFalse((self.default_session / "last_sweep").exists())

    def test_star_allows_every_session(self):
        self.write_config({"sessions": ["*"]})
        self.use_cao_socket()
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = main(["list"])
        self.assertEqual(code, 0)
        self.assertNotIn("not enabled", err.getvalue())

    # -- Logging includes the herdr session name. --

    def test_log_line_includes_the_herdr_session_name(self):
        self.write_config({"sessions": ["cao"]})
        self.use_cao_socket()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["open-picker"]), 0)
        log_text = (self.root / "shelf.log").read_text()
        self.assertIn("[cao]", log_text)

    def test_log_line_includes_the_default_session_name(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["pick"]), 0)
        log_text = (self.root / "shelf.log").read_text()
        self.assertIn("[default]", log_text)


class MigrationTest(unittest.TestCase):
    """The one-time move of pre-0.3.0 root-level state into sessions/default/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "HERDR_PLUGIN_STATE_DIR": self.tmp.name,
            "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "xdg-config"),
            "XDG_STATE_HOME": os.path.join(self.tmp.name, "xdg-state"),
        })
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HERDR_SOCKET_PATH", None)
        os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
        self.addCleanup(MainTest._reset_shelf_logger)
        self.root = Path(self.tmp.name)

    def write_legacy_state(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "activity.json").write_text('{"claude:S": {"first_seen": "2026-09-01T00:00:00Z"}}')
        archive_dir = self.root / "archive" / "20260101T000000Z-abcdef"
        archive_dir.mkdir(parents=True)
        (archive_dir / "record.json").write_text(json.dumps({
            "id": "20260101T000000Z-abcdef", "archived_at": "2026-01-01T00:00:00Z",
            "tab": {"label": "demo"}, "workspace": {"label": None}, "panes": {},
        }))
        (self.root / "last_sweep").write_text("2026-09-01T00:00:00Z\n")
        (self.root / "installed_at").write_text("2026-08-01T00:00:00Z\n")

    def test_migration_moves_legacy_files_into_sessions_default(self):
        self.write_legacy_state()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["list"]), 0)
        self.assertIn("20260101T000000Z-abcdef  demo", out.getvalue())
        default_session = self.root / "sessions" / "default"
        self.assertTrue((default_session / "activity.json").exists())
        self.assertTrue((default_session / "archive" / "20260101T000000Z-abcdef" / "record.json").exists())
        self.assertTrue((default_session / "last_sweep").exists())
        self.assertTrue((default_session / "installed_at").exists())
        self.assertFalse((self.root / "activity.json").exists())
        self.assertFalse((self.root / "archive").exists())
        self.assertFalse((self.root / "last_sweep").exists())
        self.assertFalse((self.root / "installed_at").exists())
        log_text = (self.root / "shelf.log").read_text()
        self.assertEqual(log_text.count("migrated legacy state"), 1)

    def test_migration_is_a_noop_the_second_time(self):
        self.write_legacy_state()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["list"]), 0)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["list"]), 0)
        log_text = (self.root / "shelf.log").read_text()
        self.assertEqual(log_text.count("migrated legacy state"), 1)

    def test_fresh_install_creates_no_sessions_directory(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["list"]), 0)
        self.assertIn("No archived tabs.", out.getvalue())
        self.assertFalse((self.root / "sessions").exists())


if __name__ == "__main__":
    unittest.main()
