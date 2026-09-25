import json
import tempfile
import unittest
from pathlib import Path

from shelf import config


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


if __name__ == "__main__":
    unittest.main()
