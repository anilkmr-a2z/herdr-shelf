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
        self.assertEqual(m["id"], "anilkmr.shelf")
        self.assertEqual(m["version"], shelf.__version__)
        self.assertEqual(m["min_herdr_version"], "0.9.0")
        self.assertEqual({a["id"] for a in m["actions"]}, {"restore", "sweep-now"})
        self.assertEqual({e["on"] for e in m["events"]},
                         {"pane.agent_status_changed", "tab.focused", "workspace.focused"})
        self.assertEqual(m["panes"][0]["id"], "picker")
        self.assertEqual(m["panes"][0]["placement"], "popup")


if __name__ == "__main__":
    unittest.main()
