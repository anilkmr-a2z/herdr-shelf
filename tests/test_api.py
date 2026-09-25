import os
import unittest
from unittest import mock

from shelf.api import Client, HerdrError
from tests.fakeherdr import FakeError, FakeHerdr


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHerdr()
        self.addCleanup(self.fake.close)

    def test_returns_result(self):
        self.fake.handlers["tab.list"] = lambda p: {"type": "tab_list", "tabs": [{"tab_id": "w1:t1"}]}
        self.assertEqual(Client(self.fake.path).call("tab.list")["tabs"][0]["tab_id"], "w1:t1")
        self.assertEqual(self.fake.calls, [("tab.list", {})])

    def test_passes_params(self):
        self.fake.handlers["pane.get"] = lambda p: {"pane": {"pane_id": p["pane_id"]}}
        result = Client(self.fake.path).call("pane.get", {"pane_id": "w1:p2"})
        self.assertEqual(result["pane"]["pane_id"], "w1:p2")

    def test_error_raises_with_code(self):
        def boom(p):
            raise FakeError("confirmation_required", "worktree group")

        self.fake.handlers["tab.close"] = boom
        with self.assertRaises(HerdrError) as ctx:
            Client(self.fake.path).call("tab.close", {"tab_id": "w1:t1"})
        self.assertEqual(ctx.exception.code, "confirmation_required")

    def test_unavailable_socket(self):
        with self.assertRaises(HerdrError) as ctx:
            Client("/nonexistent/herdr.sock").call("tab.list")
        self.assertEqual(ctx.exception.code, "unavailable")

    def test_missing_socket_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HerdrError) as ctx:
                Client()
        self.assertEqual(ctx.exception.code, "no_socket")


if __name__ == "__main__":
    unittest.main()
