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
        ]
        for bad in bad_values:
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as d:
                self.write(d, bad)
                with self.assertRaises(config.ConfigError):
                    config.load(d)


if __name__ == "__main__":
    unittest.main()
