import json
import logging
import tempfile
import unittest
from pathlib import Path

from shelf import config


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class ConfigTest(unittest.TestCase):
    def write(self, d, obj):
        text = obj if isinstance(obj, str) else json.dumps(obj)
        (Path(d) / "config.json").write_text(text)

    def test_defaults_without_dir_or_file(self):
        self.assertEqual(config.load(None)["mode"], "dry-run")
        with tempfile.TemporaryDirectory() as d:
            cfg = config.load(d)
        self.assertEqual(
            (cfg["idle_days"], cfg["mode"], cfg["sweep_interval_minutes"], cfg["keep_transcripts"], cfg["agents"]),
            (7, "dry-run", 60, True, {}),
        )

    def test_overrides_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, {"idle_days": 3, "mode": "live", "colour": "blue",
                           "agents": {"qwen": {"relaunch": "plain"}}})
            cfg = config.load(d)
        self.assertEqual(cfg["idle_days"], 3)
        self.assertEqual(cfg["mode"], "live")
        self.assertNotIn("colour", cfg)
        self.assertEqual(cfg["agents"], {"qwen": {"relaunch": "plain"}})

    def test_defaults_are_not_shared_between_loads(self):
        cfg = config.load(None)
        cfg["agents"]["x"] = {}
        self.assertEqual(config.load(None)["agents"], {})

    def test_malformed_json(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, "{not json")
            with self.assertRaises(config.ConfigError):
                config.load(d)

    def test_invalid_values(self):
        bad_values = [
            {"mode": "yes"}, {"idle_days": 0}, {"idle_days": True}, {"keep_transcripts": "no"},
            {"agents": []}, {"agents": {"x": 1}}, {"sweep_interval_minutes": -1}, [1, 2],
            '{"idle_days": Infinity}', '{"idle_days": NaN}', {"idle_days": 100000},
            {"agents": {"x": {"resume": "--load {id}"}}},
            {"agents": {"x": {"relaunch": "fancy"}}},
            {"agents": {"x": {"strip": "-r"}}},
            {"agents": {"x": {"program": ""}}},
            {"agents": {"x": {"resume": ["--load"]}}},
        ]
        for bad in bad_values:
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as d:
                self.write(d, bad)
                with self.assertRaises(config.ConfigError):
                    config.load(d)

    def test_unknown_top_level_keys_are_warned_not_failed(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, {"colour": "blue"})
            with self.assertLogs("shelf", level="WARNING") as cm:
                cfg = config.load(d)
        self.assertNotIn("colour", cfg)
        self.assertTrue(any("colour" in m for m in cm.output))

    def test_unknown_agent_keys_are_warned_not_failed(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, {"agents": {"qwen": {"relaunch": "plain", "nickname": "Q"}}})
            with self.assertLogs("shelf", level="WARNING") as cm:
                cfg = config.load(d)
        self.assertEqual(cfg["agents"], {"qwen": {"relaunch": "plain", "nickname": "Q"}})
        self.assertTrue(any("nickname" in m for m in cm.output))

    def test_resume_must_contain_the_id_placeholder(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, {"agents": {"x": {"program": "x", "resume": ["--load", "now"]}}})
            with self.assertRaises(config.ConfigError):
                config.load(d)

    def test_resume_with_id_placeholder_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, {"agents": {"x": {"program": "x", "resume": ["--load", "{id}"]}}})
            cfg = config.load(d)
        self.assertEqual(cfg["agents"]["x"]["resume"], ["--load", "{id}"])

    def test_sessions_defaults_to_default_only(self):
        self.assertEqual(config.load(None)["sessions"], ["default"])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(config.load(d)["sessions"], ["default"])

    def test_sessions_override(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, {"sessions": ["default", "work"]})
            cfg = config.load(d)
        self.assertEqual(cfg["sessions"], ["default", "work"])

    def test_sessions_star_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, {"sessions": ["*"]})
            cfg = config.load(d)
        self.assertEqual(cfg["sessions"], ["*"])

    def test_sessions_invalid_values(self):
        bad_values = [
            {"sessions": []},
            {"sessions": "default"},
            {"sessions": [""]},
            {"sessions": [1]},
            {"sessions": None},
            {"sessions": {"default": True}},
        ]
        for bad in bad_values:
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as d:
                self.write(d, bad)
                with self.assertRaises(config.ConfigError):
                    config.load(d)

    def test_load_warn_false_suppresses_unknown_key_warning(self):
        handler = _CapturingHandler()
        log = logging.getLogger("shelf")
        log.addHandler(handler)
        try:
            with tempfile.TemporaryDirectory() as d:
                self.write(d, {"colour": "blue"})
                cfg = config.load(d, warn=False)
        finally:
            log.removeHandler(handler)
        self.assertNotIn("colour", cfg)
        self.assertEqual(handler.records, [])

    def test_load_warn_false_suppresses_unknown_agent_key_warning(self):
        handler = _CapturingHandler()
        log = logging.getLogger("shelf")
        log.addHandler(handler)
        try:
            with tempfile.TemporaryDirectory() as d:
                self.write(d, {"agents": {"qwen": {"relaunch": "plain", "nickname": "Q"}}})
                cfg = config.load(d, warn=False)
        finally:
            log.removeHandler(handler)
        self.assertEqual(cfg["agents"], {"qwen": {"relaunch": "plain", "nickname": "Q"}})
        self.assertEqual(handler.records, [])

    def test_load_warn_false_still_raises_on_invalid_values(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, {"idle_days": 0})
            with self.assertRaises(config.ConfigError):
                config.load(d, warn=False)


class SessionEnabledTest(unittest.TestCase):
    def test_default_session_enabled_by_default(self):
        cfg = config.load(None)
        self.assertTrue(config.session_enabled(cfg, "default"))
        self.assertFalse(config.session_enabled(cfg, "cao"))

    def test_star_enables_every_session(self):
        cfg = {"sessions": ["*"]}
        self.assertTrue(config.session_enabled(cfg, "default"))
        self.assertTrue(config.session_enabled(cfg, "cao"))
        self.assertTrue(config.session_enabled(cfg, "anything"))

    def test_named_sessions_enable_only_those_listed(self):
        cfg = {"sessions": ["default", "work"]}
        self.assertTrue(config.session_enabled(cfg, "default"))
        self.assertTrue(config.session_enabled(cfg, "work"))
        self.assertFalse(config.session_enabled(cfg, "cao"))


class SessionsForGateTest(unittest.TestCase):
    """A best-effort, never-raising, never-logging read of just the
    "sessions" key, used for the per-session allowlist gate: a valid
    "sessions" value is honored even when the rest of config.json is
    invalid, since a hook must still be able to tell which session it's
    allowed to act in."""

    def write(self, d, obj):
        text = obj if isinstance(obj, str) else json.dumps(obj)
        (Path(d) / "config.json").write_text(text)

    def test_no_config_dir_or_file_uses_the_default(self):
        self.assertEqual(config.sessions_for_gate(None), ["default"])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(config.sessions_for_gate(d), ["default"])

    def test_valid_sessions_is_used_even_when_another_key_is_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, {"idle_days": -1, "sessions": ["cao"]})
            self.assertEqual(config.sessions_for_gate(d), ["cao"])

    def test_star_is_used_even_when_another_key_is_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, {"mode": "yes", "sessions": ["*"]})
            self.assertEqual(config.sessions_for_gate(d), ["*"])

    def test_invalid_sessions_value_falls_back_to_the_default(self):
        bad = [[], "default", [""], [1], None, {"default": True}]
        for value in bad:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as d:
                self.write(d, {"sessions": value})
                self.assertEqual(config.sessions_for_gate(d), ["default"])

    def test_missing_sessions_key_falls_back_to_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, {"idle_days": 3})
            self.assertEqual(config.sessions_for_gate(d), ["default"])

    def test_malformed_json_falls_back_to_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, "{not json")
            self.assertEqual(config.sessions_for_gate(d), ["default"])

    def test_non_object_top_level_falls_back_to_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, [1, 2])
            self.assertEqual(config.sessions_for_gate(d), ["default"])

    def test_never_logs_a_warning(self):
        handler = _CapturingHandler()
        log = logging.getLogger("shelf")
        log.addHandler(handler)
        try:
            with tempfile.TemporaryDirectory() as d:
                self.write(d, {"colour": "blue", "idle_days": -1, "sessions": ["cao"]})
                result = config.sessions_for_gate(d)
        finally:
            log.removeHandler(handler)
        self.assertEqual(result, ["cao"])
        self.assertEqual(handler.records, [])


if __name__ == "__main__":
    unittest.main()
