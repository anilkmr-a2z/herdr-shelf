import io
import json
import logging
import os
import signal
import tempfile
import termios
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from shelf.__main__ import main
from shelf.api import HerdrError
from shelf.util import FileLock, iso, now
from tests.fakeherdr import FakeError, FakeHerdr

# A fixed instant for tests that need main()'s internal now() to match an
# assertion made afterward: calling the real now() twice (once inside
# main(), once in the test) is flaky whenever the two calls straddle a
# second boundary, since iso() truncates to whole seconds.
FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _restorable_record(archive_id: str, session_value: str = "S1") -> dict:
    """A minimal record.json that restore.restore can actually apply: one
    claude pane with a valid session id, no splits."""
    return {
        "version": 1, "id": archive_id, "archived_at": "2026-01-01T00:00:00Z",
        "workspace": {"label": None, "cwd": None},
        "tab": {"label": "demo"},
        "layout": {"focused_pane_id": "p1", "zoomed": False,
                   "root": {"type": "pane", "pane_id": "p1"}},
        "panes": {"p1": {"cwd": None, "agent": "claude",
                         "session": {"kind": "id", "value": session_value, "source": "herdr:claude"},
                         "launch_argv": ["claude"], "last_activity": "2026-01-01T00:00:00Z"}},
        "session_copies": [],
    }


def _restore_handlers(tab_id: str = "w9:t1") -> dict:
    """FakeHerdr handlers sufficient for restore.restore to succeed: no
    matching workspace (so one is created), no live panes anywhere."""
    return {
        "workspace.list": lambda p: {"workspaces": []},
        "workspace.create": lambda p: {"type": "workspace_created",
                                       "workspace": {"workspace_id": "w9", "label": p.get("label")},
                                       "tab": {"tab_id": tab_id}, "root_pane": {"pane_id": "w9:p1"}},
        "layout.apply": lambda p: {"type": "layout_apply", "layout": {"tab_id": tab_id}},
        "pane.list": lambda p: {"panes": []},
    }


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
        for argv in (["track"], ["sweep", "--if-due"], ["open-picker"], ["open-archive"]):
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

    def test_pick_keeps_log_lines_off_the_popup_while_it_runs(self):
        seen = []

        def fake_run(arch, do_restore, now_fn, notify=None, **kwargs):
            seen.append([type(h) for h in logging.getLogger("shelf").handlers])

        with mock.patch("shelf.__main__.picker.run", side_effect=fake_run), \
                mock.patch("shelf.__main__.Client"), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["pick"]), 0)
        self.assertEqual(seen, [[logging.FileHandler, logging.NullHandler]])
        self.assertIn(logging.StreamHandler, [type(h) for h in logging.getLogger("shelf").handlers])

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


def _pane(pane_id, tab, session, status="idle"):
    return {"pane_id": pane_id, "tab_id": tab, "terminal_id": "term_" + pane_id, "cwd": "/src",
            "agent": "claude", "agent_status": status,
            "agent_session": {"agent": "claude", "kind": "id", "value": session, "source": "herdr:claude"}}


class ArchiveTabTest(unittest.TestCase):
    """open-archive (the archive-tab action) and confirm-archive (its popup)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeHerdr()
        self.addCleanup(self.fake.close)
        env = mock.patch.dict(os.environ, {
            "HERDR_PLUGIN_STATE_DIR": self.tmp.name,
            "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "xdg-config"),
            "XDG_STATE_HOME": os.path.join(self.tmp.name, "xdg-state"),
            "CLAUDE_CONFIG_DIR": os.path.join(self.tmp.name, "claude"),
            "CODEX_HOME": os.path.join(self.tmp.name, "codex"),
            "HERDR_SOCKET_PATH": self.fake.path,
            "HERDR_PLUGIN_ID": "shelf",
            "HERDR_TAB_ID": "w1:t1",
        })
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
        self.addCleanup(MainTest._reset_shelf_logger)
        self.session = Path(self.tmp.name) / "sessions" / "default"
        self.tabs = [{"tab_id": "w1:t1", "workspace_id": "w1", "label": "old", "focused": True}]
        self.panes = [_pane("w1:p1", "w1:t1", "OLD")]
        self.fake.handlers.update({
            "tab.list": lambda p: {"tabs": [dict(t) for t in self.tabs]},
            "pane.list": lambda p: {"panes": [dict(x) for x in self.panes]},
            "workspace.list": lambda p: {"workspaces": [{"workspace_id": "w1", "label": "main"}]},
            "notification.show": lambda p: {"type": "ok"},
            "plugin.pane.open": lambda p: {"type": "ok"},
            "layout.export": self.layout_export,
            "pane.process_info": lambda p: {"process_info": {"foreground_processes": [{"name": "claude",
                                                                                      "argv": ["claude"]}]}},
            "tab.close": self.close_tab,
        })
        (self.session / "installed_at").parent.mkdir(parents=True)
        (self.session / "installed_at").write_text("2026-01-01T00:00:00Z\n")
        (self.session / "activity.json").write_text(json.dumps(
            {"claude:OLD": {"first_seen": "2026-01-01T00:00:00Z",
                            "last_active": iso(now() - timedelta(days=3, hours=1))}}))

    def layout_export(self, p):
        pane_id = next(x["pane_id"] for x in self.panes if x["tab_id"] == p["tab_id"])
        return {"layout": {"workspace_id": "w1", "tab_id": p["tab_id"], "zoomed": False,
                           "focused_pane_id": pane_id, "root": {"type": "pane", "pane_id": pane_id, "cwd": "/src"}}}

    def close_tab(self, p):
        self.tabs = [t for t in self.tabs if t["tab_id"] != p["tab_id"]]
        self.panes = [x for x in self.panes if x["tab_id"] != p["tab_id"]]
        return {"type": "ok"}

    def notifications(self):
        return [params["body"] for method, params in self.fake.calls if method == "notification.show"]

    def opened(self):
        return [params for method, params in self.fake.calls if method == "plugin.pane.open"]

    def run_main(self, argv):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = main(argv)
        self.assertEqual(code, 0)
        self.err = err.getvalue()
        return out.getvalue()

    def write_config(self, obj):
        config_dir = os.path.join(self.tmp.name, "config")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.json"), "w") as f:
            json.dump(obj, f)
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = config_dir

    def leave_migration_incomplete(self):
        (Path(self.tmp.name) / "activity.json").write_text("{}")
        return mock.patch("shelf.__main__.migrate.merge_into_default_session")

    # -- open-archive --

    def test_open_archive_opens_the_popup_with_the_question(self):
        self.run_main(["open-archive"])
        text = "\n".join([' Archive "old"?', " Last activity 3 days ago.",
                          " The tab closes; the restore picker brings it back.", " y archive   any other key cancel"])
        self.assertEqual(self.opened(), [{
            "plugin_id": "shelf", "entrypoint": "archive-confirm", "height": 7,
            "env": {"SHELF_TAB_ID": "w1:t1", "SHELF_TAB_LABEL": "old", "SHELF_TAB_TERMINALS": "term_w1:p1",
                    "SHELF_CONFIRM_TEXT": text}}])
        self.assertEqual(self.notifications(), [])

    def test_open_archive_shows_warnings_in_the_popup(self):
        self.panes[0]["agent_status"] = "working"
        self.run_main(["open-archive"])
        (opened,) = self.opened()
        self.assertIn(" A pane is still working; archiving stops it.", opened["env"]["SHELF_CONFIRM_TEXT"])
        self.assertEqual(opened["height"], 8)

    def test_open_archive_notifies_a_block_and_opens_nothing(self):
        self.panes[0]["agent_session"]["value"] = "-rf"
        self.run_main(["open-archive"])
        self.assertEqual(self.opened(), [])
        self.assertEqual(self.notifications(), ['shelf: can\'t archive "old": w1:p1: invalid session id'])

    def test_open_archive_without_a_tab_id(self):
        os.environ.pop("HERDR_TAB_ID")
        self.run_main(["open-archive"])
        self.assertEqual(self.notifications(), ["shelf: no tab to archive"])

    def test_open_archive_for_a_tab_that_is_gone(self):
        os.environ["HERDR_TAB_ID"] = "w1:t404"
        self.run_main(["open-archive"])
        self.assertEqual(self.notifications(), ["shelf: can't archive this tab: no tab w1:t404"])

    def test_open_archive_when_a_popup_is_already_open(self):
        def busy(_params):
            raise FakeError("ui_busy", "busy")

        self.fake.handlers["plugin.pane.open"] = busy
        self.run_main(["open-archive"])
        self.assertEqual(self.notifications(), ["shelf: close the open popup or dialog first"])

    def test_open_archive_when_the_popup_cannot_be_opened(self):
        def refuse(_params):
            raise FakeError("invalid_params", "no such entrypoint")

        self.fake.handlers["plugin.pane.open"] = refuse
        self.run_main(["open-archive"])
        (note,) = self.notifications()
        self.assertTrue(note.startswith("shelf: can't archive this tab: "), note)
        self.assertIn("no such entrypoint", note)

    def test_open_archive_with_an_invalid_config(self):
        self.write_config({"idle_days": -1})
        self.run_main(["open-archive"])
        self.assertEqual(self.opened(), [])
        (note,) = self.notifications()
        self.assertTrue(note.startswith("shelf: can't archive this tab: "), note)

    def test_open_archive_while_a_migration_is_incomplete(self):
        with self.leave_migration_incomplete():
            self.run_main(["open-archive"])
        self.assertEqual(self.opened(), [])
        self.assertEqual(self.notifications(),
                         ["shelf: migrating state from an older version; try again in a moment"])

    # -- confirm-archive --

    def confirm(self, key=None, tab_id="w1:t1", terminals="term_w1:p1", read_error=None):
        os.environ.update({"SHELF_TAB_ID": tab_id, "SHELF_TAB_LABEL": "old", "SHELF_TAB_TERMINALS": terminals,
                           "SHELF_CONFIRM_TEXT": ' Archive "old"?'})
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in
                                 ("SHELF_TAB_ID", "SHELF_TAB_LABEL", "SHELF_TAB_TERMINALS", "SHELF_CONFIRM_TEXT")])
        with mock.patch("shelf.__main__.confirm.read_key", return_value=key, side_effect=read_error), \
                mock.patch("shelf.__main__.signal.signal") as self.set_signal:
            return self.run_main(["confirm-archive"])

    def test_confirm_archive_y_archives(self):
        out = self.confirm(b"y")
        self.assertIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)
        self.assertEqual(self.notifications(), ['shelf: archived "old"'])
        self.assertEqual(out, ' Archive "old"?\n Archiving...')
        self.assertEqual(self.err, "")
        self.assertEqual(self.set_signal.call_args_list,
                         [mock.call(signal.SIGINT, signal.SIG_IGN), mock.call(signal.SIGHUP, signal.SIG_IGN)])

    def test_confirm_archive_capital_y_archives(self):
        self.confirm(b"Y")
        self.assertIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)

    def test_confirm_archive_archives_through_warnings(self):
        self.panes[0]["agent_status"] = "working"
        self.confirm(b"y")
        self.assertIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)

    def test_confirm_archive_any_other_key_cancels(self):
        for key in (b"n", b"\x1b", b"\x03", b""):
            with self.subTest(key=key):
                self.fake.calls.clear()
                out = self.confirm(key)
                self.assertNotIn("tab.close", self.fake.methods())
                self.assertEqual(self.notifications(), [])
                self.assertNotIn("Archiving", out)
                self.set_signal.assert_not_called()

    def test_confirm_archive_when_the_key_cannot_be_read(self):
        self.confirm(read_error=termios.error(25, "Inappropriate ioctl for device"))
        self.assertNotIn("tab.close", self.fake.methods())
        (note,) = self.notifications()
        self.assertTrue(note.startswith('shelf: can\'t archive "old": '), note)
        self.set_signal.assert_not_called()

    def test_confirm_archive_ctrl_c_before_a_key_cancels_quietly(self):
        self.confirm(read_error=KeyboardInterrupt)
        self.assertNotIn("tab.close", self.fake.methods())
        self.assertEqual(self.notifications(), [])

    def test_confirm_archive_with_an_invalid_config(self):
        self.write_config({"idle_days": -1})
        self.confirm(b"y")
        self.assertNotIn("tab.close", self.fake.methods())
        (note,) = self.notifications()
        self.assertTrue(note.startswith('shelf: can\'t archive "old": '), note)

    def test_confirm_archive_while_a_migration_is_incomplete(self):
        with self.leave_migration_incomplete():
            self.confirm(b"y")
        self.assertNotIn("tab.close", self.fake.methods())
        self.assertEqual(self.notifications(),
                         ["shelf: migrating state from an older version; try again in a moment"])

    def test_confirm_archive_keeps_a_refusal_out_of_the_popup_and_without_a_traceback(self):
        self.confirm(b"y", terminals="term_gone")
        self.assertEqual(self.err, "")
        log_text = (Path(self.tmp.name) / "shelf.log").read_text()
        self.assertIn("can't archive old: tab changed", log_text)
        self.assertNotIn("Traceback", log_text)

    def test_confirm_archive_finds_the_tab_after_its_id_shifts(self):
        self.confirm(b"y", tab_id="w1:t7")
        self.assertIn(("tab.close", {"tab_id": "w1:t1"}), self.fake.calls)

    def test_confirm_archive_when_the_tab_is_gone(self):
        self.confirm(b"y", terminals="term_gone")
        self.assertNotIn("tab.close", self.fake.methods())
        self.assertEqual(self.notifications(), ['shelf: can\'t archive "old": tab changed'])

    def test_confirm_archive_notifies_a_block(self):
        self.panes[0]["agent_session"]["value"] = "-rf"
        self.confirm(b"y")
        self.assertNotIn("tab.close", self.fake.methods())
        self.assertEqual(self.notifications(), ['shelf: can\'t archive "old": w1:p1: invalid session id'])

    def test_confirm_archive_while_a_sweep_holds_the_lock(self):
        with mock.patch("shelf.sweep.ARCHIVE_NOW_LOCK_WAIT_SECONDS", 0.1), FileLock(self.session / "sweep.lock"):
            self.confirm(b"y")
        self.assertNotIn("tab.close", self.fake.methods())
        self.assertEqual(self.notifications(), ["shelf: a sweep is running; try again in a moment"])


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
        # A real, well-formed pane.agent_status_changed payload -- with a
        # working pane.get behind it -- so that if the gate were ever
        # removed, activity.track would genuinely succeed and write
        # activity.json. Without a real payload this test would still pass
        # even with the gate removed, since track() is a no-op on an empty
        # event regardless -- see the mutation checks in the commit body.
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        fake.handlers["pane.get"] = lambda p: {
            "pane": {"agent_session": {"agent": "claude", "kind": "id", "value": "S1", "source": "herdr:claude"}}}
        self.use_cao_socket_linked_to(fake)
        event_json = json.dumps({"event": "pane_agent_status_changed",
                                 "data": {"pane_id": "p1", "agent_status": "done"}})
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": event_json, "HERDR_PANE_ID": "p1"}), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(main(["track"]), 0)
        self.assertFalse(self.cao_session.exists())
        self.assertNotIn("pane.get", fake.methods())

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

    def test_disabled_open_and_confirm_archive_show_a_notification_and_archive_nothing(self):
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        fake.handlers["notification.show"] = lambda p: {"type": "ok"}
        self.use_cao_socket_linked_to(fake)
        for argv in (["open-archive"], ["confirm-archive"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                self.assertEqual(main(argv), 0)
        self.assertEqual(fake.methods(), ["notification.show", "notification.show"])
        self.assertFalse(self.cao_session.exists())

    # -- Manual commands: a disabled session prints a message and exits 1. --

    def test_disabled_manual_sweep_prints_message_and_exits_one(self):
        self.use_cao_socket()
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = main(["sweep"])
        self.assertEqual(code, 1)
        self.assertIn("shelf is not enabled for herdr session 'cao'", err.getvalue())
        self.assertIn('add it to "sessions" in config.json', err.getvalue())

    def test_disabled_manual_sweep_prints_message_even_with_no_socket_path_at_all(self):
        # The best-effort notification must never swallow the printed
        # message: Client() itself raises here (no HERDR_SOCKET_PATH), and
        # that must not stop the message below it from being printed.
        self.write_config({"sessions": ["work"]})
        os.environ.pop("HERDR_SOCKET_PATH", None)  # -> "default", not "work"
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = main(["sweep"])
        self.assertEqual(code, 1)
        self.assertIn("shelf is not enabled for herdr session 'default'", err.getvalue())

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

    def test_unparseable_session_path_is_always_disabled_even_with_star(self):
        # A socket path whose tail is exactly sessions/<X>/herdr.sock, but
        # where <X> fails herdr's session name rule (a space, here),
        # resolves to None, which must never be enabled -- not even by "*".
        self.write_config({"sessions": ["*"]})
        path = os.path.join(self.tmp.name, "herdr-config", "sessions", "bad name", "herdr.sock")
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": path}):
            err = io.StringIO()
            with redirect_stderr(err):
                code = main(["list"])
        self.assertEqual(code, 1)
        self.assertIn("shelf is not enabled for herdr session 'unknown'", err.getvalue())

    def test_unparseable_session_path_logs_with_the_unknown_tag(self):
        # open-picker's disabled path calls notification.show; with no real
        # socket behind this path, that failure is what actually produces a
        # log line to check the session tag on.
        path = os.path.join(self.tmp.name, "herdr-config", "sessions", "bad name", "herdr.sock")
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": path}), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["open-picker"]), 0)
        log_text = (self.root / "shelf.log").read_text()
        self.assertIn("[unknown]", log_text)

    def test_ancestor_sessions_directory_uses_the_default_session(self):
        # "sessions" here is an unrelated ancestor directory, nowhere near
        # the end of the path -- this must behave exactly like the default
        # session, not be disabled.
        path = os.path.join(self.tmp.name, "sessions", "xdg", "herdr", "herdr.sock")
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": path}):
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = main(["list"])
        self.assertEqual(code, 0)
        self.assertNotIn("not enabled", err.getvalue())

    # -- Mutation-resistance: each of these fails if the corresponding
    # per-session wiring is broken, not just if the gate is broken. --

    def test_restore_records_activity_under_the_session_dir_not_the_root(self):
        self.write_config({"sessions": ["default", "cao"]})
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        fake.handlers.update(_restore_handlers())
        self.use_cao_socket_linked_to(fake)
        archive_dir = self.cao_session / "archive" / "20260101T000000Z-abcdef"
        archive_dir.mkdir(parents=True)
        (archive_dir / "record.json").write_text(json.dumps(_restorable_record("20260101T000000Z-abcdef")))

        # main()'s own now() is fixed so the timestamp it writes can be
        # compared exactly, rather than calling the real now() again here
        # and risking the two straddling a second boundary.
        with mock.patch("shelf.__main__.now", return_value=FIXED_NOW), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(["restore", "20260101T000000Z-abcdef"])

        self.assertEqual(code, 0)
        session_activity = json.loads((self.cao_session / "activity.json").read_text())
        self.assertEqual(session_activity["claude:S1"]["restored_at"], iso(FIXED_NOW))
        self.assertFalse((self.root / "activity.json").exists())

    def test_pick_lists_archives_from_the_session_dir_not_the_root(self):
        self.write_config({"sessions": ["default", "cao"]})
        self.use_cao_socket()
        archive_dir = self.cao_session / "archive" / "20260101T000000Z-abcdef"
        archive_dir.mkdir(parents=True)
        (archive_dir / "record.json").write_text(json.dumps({
            "id": "20260101T000000Z-abcdef", "archived_at": "2026-01-01T00:00:00Z",
            "tab": {"label": "distinctive-label"}, "workspace": {"label": None}, "panes": {},
        }))
        listed = []
        with mock.patch("shelf.__main__.picker.run",
                        side_effect=lambda arch, *a, **k: listed.extend(arch.list())), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(main(["pick"]), 0)
        self.assertEqual([r["tab"]["label"] for r in listed], ["distinctive-label"])

    def test_invalid_config_fallback_does_not_enable_other_sessions(self):
        # "sessions" is absent, and idle_days is invalid: the gate's
        # fallback must be exactly ["default"], not something permissive
        # like ["*"] -- "cao" must stay disabled.
        self.write_config({"idle_days": -1})
        self.use_cao_socket()
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["list"])
        self.assertEqual(code, 1)
        self.assertIn("shelf is not enabled for herdr session 'cao'", err.getvalue())

    # -- Logging includes the herdr session name. --

    def test_log_line_includes_the_herdr_session_name(self):
        self.write_config({"sessions": ["cao"]})
        self.use_cao_socket()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["open-picker"]), 0)
        log_text = (self.root / "shelf.log").read_text()
        self.assertIn("[cao]", log_text)

    def test_log_line_includes_the_default_session_name(self):
        with mock.patch("builtins.input", side_effect=EOFError), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
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

    def test_migrated_archive_can_be_restored(self):
        # Not just listable (test_migration_moves_legacy_files_into_...
        # above) but actually restorable, end to end, from sessions/default.
        self.root.mkdir(parents=True, exist_ok=True)
        archive_dir = self.root / "archive" / "20260101T000000Z-abcdef"
        archive_dir.mkdir(parents=True)
        (archive_dir / "record.json").write_text(json.dumps(_restorable_record("20260101T000000Z-abcdef")))
        fake = FakeHerdr()
        self.addCleanup(fake.close)
        fake.handlers.update(_restore_handlers())
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path}):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = main(["restore", "20260101T000000Z-abcdef"])
        self.assertEqual(code, 0)
        self.assertIn("w9:t1", out.getvalue())
        self.assertFalse((self.root / "sessions" / "default" / "archive" / "20260101T000000Z-abcdef").exists())
        self.assertFalse((self.root / "archive").exists())

    def test_rollback_then_upgrade_migrates_the_new_archive_too(self):
        # First upgrade: migrate one archive via a real command.
        self.write_legacy_state()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["list"]), 0)
        self.assertFalse((self.root / "archive").exists())

        # Rollback to 0.2.x (which only knows the root-level layout) and use
        # it: it archives a second tab directly at the root.
        second = self.root / "archive" / "20260102T000000Z-bbbbbb"
        second.mkdir(parents=True)
        (second / "record.json").write_text(json.dumps({
            "id": "20260102T000000Z-bbbbbb", "archived_at": "2026-01-02T00:00:00Z",
            "tab": {"label": "also-old"}, "workspace": {"label": None}, "panes": {},
        }))

        # Upgrade again: a later command must still pick up the new one.
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["list"]), 0)
        self.assertIn("20260102T000000Z-bbbbbb  also-old", out.getvalue())
        self.assertIn("20260101T000000Z-abcdef  demo", out.getvalue())
        self.assertFalse((self.root / "archive").exists())


class MigrationIncompleteGateTest(unittest.TestCase):
    """If the migration attempt could not fully clear activity.json,
    installed_at or last_sweep from the root (for example a still-running
    0.2.x process holds the root locks), sweep and archive must not run
    against a possibly-incomplete activity history; list (read-only) may
    still proceed."""

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

    def leave_migration_incomplete(self):
        """Simulate a migration attempt that could not fully clear the
        root: merge_into_default_session is stubbed out (its own lock
        behavior is covered separately in tests/test_migrate.py) and a
        root-level activity.json is left in place, as it would be if a
        0.2.x process still held the root locks."""
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "activity.json").write_text("{}")
        return mock.patch("shelf.__main__.migrate.merge_into_default_session")

    def test_sweep_hook_skips_silently_with_one_log_line(self):
        with self.leave_migration_incomplete():
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["sweep", "--if-due"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertFalse((self.root / "sessions" / "default" / "last_sweep").exists())
        log_text = (self.root / "shelf.log").read_text()
        self.assertEqual(log_text.count("migrating state"), 1)

    def test_manual_sweep_prints_message_and_exits_one(self):
        with self.leave_migration_incomplete():
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = main(["sweep"])
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue().strip(),
                         "shelf: migrating state from an older version; try again in a moment")

    def test_archive_prints_message_and_exits_one(self):
        with self.leave_migration_incomplete():
            err = io.StringIO()
            with redirect_stderr(err):
                code = main(["archive", "w1:t1"])
        self.assertEqual(code, 1)
        self.assertIn("shelf: migrating state from an older version; try again in a moment", err.getvalue())

    def test_list_still_proceeds(self):
        with self.leave_migration_incomplete():
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["list"])
        self.assertEqual(code, 0)
        self.assertIn("No archived tabs.", out.getvalue())

    def test_track_is_unaffected(self):
        # Only sweep and archive are gated on a finished migration; other
        # commands are unaffected by this specific check.
        with self.leave_migration_incomplete(), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["track"]), 0)

    def test_gate_lifts_once_installed_at_is_the_only_leftover(self):
        # The check covers activity.json, installed_at and last_sweep
        # individually -- installed_at alone is enough to gate.
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "installed_at").write_text("2026-01-01T00:00:00Z\n")
        with mock.patch("shelf.__main__.migrate.merge_into_default_session"):
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = main(["sweep"])
        self.assertEqual(code, 1)
        self.assertIn("migrating state", err.getvalue())

    def test_leftover_archive_dir_alone_does_not_gate(self):
        # archive/ can legitimately remain at the root forever (an
        # unresolved id collision -- see shelf.migrate); it must not block
        # sweep/archive on its own.
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "archive.conflict" / "some-id").mkdir(parents=True)
        (self.root / "archive" / "some-id").mkdir(parents=True)
        with mock.patch("shelf.__main__.migrate.merge_into_default_session"):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["sweep"])
        # Not gated by the migrating-state check; whatever happens next
        # (herdr unreachable in this test) is unrelated to this gate.
        self.assertNotIn("migrating state", err.getvalue())


if __name__ == "__main__":
    unittest.main()
