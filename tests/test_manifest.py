import unittest
from pathlib import Path

import shelf

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
class ManifestTest(unittest.TestCase):
    def test_manifest(self):
        path = Path(__file__).resolve().parent.parent / "herdr-plugin.toml"
        m = tomllib.loads(path.read_text())
        self.assertEqual(m["id"], "shelf")
        self.assertEqual(m["version"], shelf.__version__)
        self.assertEqual(m["min_herdr_version"], "0.9.0")
        self.assertEqual({a["id"] for a in m["actions"]}, {"restore", "archive-tab", "sweep-now"})
        archive_tab = next(a for a in m["actions"] if a["id"] == "archive-tab")
        self.assertEqual(archive_tab["contexts"], ["tab"])
        self.assertEqual(archive_tab["command"], ["python3", "-m", "shelf", "open-archive"])
        self.assertEqual({e["on"] for e in m["events"]},
                         {"pane.agent_status_changed", "pane.agent_detected", "workspace.focused"})
        self.assertEqual(m["panes"][0]["id"], "picker")
        self.assertEqual(m["panes"][0]["placement"], "popup")
        self.assertEqual((m["panes"][0]["width"], m["panes"][0]["height"]), ("80%", 18))
        confirm = m["panes"][1]
        self.assertEqual((confirm["id"], confirm["placement"], confirm["width"], confirm["height"]),
                         ("archive-confirm", "popup", 64, 8))
        self.assertEqual(confirm["command"], ["python3", "-m", "shelf", "confirm-archive"])


if __name__ == "__main__":
    unittest.main()
