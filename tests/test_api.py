import os
import socket
import tempfile
import threading
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
        self.assertTrue(ctx.exception.definite)

    def test_unavailable_socket(self):
        with self.assertRaises(HerdrError) as ctx:
            Client("/nonexistent/herdr.sock").call("tab.list")
        self.assertEqual(ctx.exception.code, "unavailable")
        self.assertTrue(ctx.exception.definite)

    def test_missing_socket_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HerdrError) as ctx:
                Client()
        self.assertEqual(ctx.exception.code, "no_socket")
        self.assertTrue(ctx.exception.definite)

    def test_handler_exception_becomes_fake_handler_error_and_server_keeps_serving(self):
        def boom(p):
            raise RuntimeError("kaboom")

        self.fake.handlers["boom"] = boom
        self.fake.handlers["ping"] = lambda p: {"ok": True}
        with self.assertRaises(HerdrError) as ctx:
            Client(self.fake.path).call("boom")
        self.assertEqual(ctx.exception.code, "fake_handler_error")
        self.assertEqual(len(self.fake.errors), 1)
        self.assertIsInstance(self.fake.errors[0], RuntimeError)
        result = Client(self.fake.path).call("ping")
        self.assertEqual(result, {"ok": True})

    def test_close_without_reply_is_empty_response(self):
        d = tempfile.mkdtemp(prefix="shelf-")
        path = os.path.join(d, "h.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)

        def serve_once():
            conn, _ = server.accept()
            with conn:
                conn.recv(65536)

        t = threading.Thread(target=serve_once)
        t.start()
        try:
            with self.assertRaises(HerdrError) as ctx:
                Client(path).call("tab.list")
            self.assertEqual(ctx.exception.code, "empty_response")
            self.assertFalse(ctx.exception.definite)
        finally:
            t.join(5)
            server.close()
            os.unlink(path)
            os.rmdir(d)

    def test_large_reply_round_trips(self):
        big = "x" * 100000
        self.fake.handlers["big"] = lambda p: {"data": big}
        result = Client(self.fake.path).call("big")
        self.assertEqual(len(result["data"]), 100000)
        self.assertEqual(result["data"], big)


if __name__ == "__main__":
    unittest.main()
