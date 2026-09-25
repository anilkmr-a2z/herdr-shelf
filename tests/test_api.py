import json
import os
import socket
import tempfile
import threading
import time
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
        # This test deliberately triggers a handler error; close() now asserts
        # on leftover errors, so acknowledge it was expected before teardown.
        self.fake.errors.clear()

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

    def test_close_raises_when_errors_recorded(self):
        fake = FakeHerdr()
        fake.errors.append(RuntimeError("boom"))
        with self.assertRaises(AssertionError):
            fake.close()
        # Cleanup must still have happened despite the raise.
        self.assertFalse(os.path.exists(fake.path))
        self.assertFalse(os.path.exists(os.path.dirname(fake.path)))

    def test_malformed_request_replies_unknown_id_and_keeps_serving(self):
        self.fake.handlers["ping"] = lambda p: {"ok": True}
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(self.fake.path)
        sock.sendall(b"not json\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        sock.close()
        resp = json.loads(buf)
        self.assertEqual(resp["id"], "unknown")
        self.assertEqual(resp["error"]["code"], "fake_handler_error")
        self.assertEqual(len(self.fake.errors), 1)
        self.fake.errors.clear()
        result = Client(self.fake.path).call("ping")
        self.assertEqual(result, {"ok": True})

    def test_unserializable_result_records_error_and_keeps_serving(self):
        self.fake.handlers["bad"] = lambda p: {"nope": object()}
        self.fake.handlers["ping"] = lambda p: {"ok": True}
        with self.assertRaises(HerdrError) as ctx:
            Client(self.fake.path).call("bad")
        self.assertEqual(ctx.exception.code, "fake_handler_error")
        self.assertEqual(len(self.fake.errors), 1)
        self.fake.errors.clear()
        result = Client(self.fake.path).call("ping")
        self.assertEqual(result, {"ok": True})

    def test_truncated_reply_is_bad_response(self):
        d = tempfile.mkdtemp(prefix="shelf-")
        path = os.path.join(d, "h.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)

        def serve_once():
            conn, _ = server.accept()
            with conn:
                conn.recv(65536)
                conn.sendall(b'{"id": "x", "res')

        t = threading.Thread(target=serve_once)
        t.start()
        try:
            with self.assertRaises(HerdrError) as ctx:
                Client(path).call("tab.list")
            self.assertEqual(ctx.exception.code, "bad_response")
            self.assertFalse(ctx.exception.definite)
        finally:
            t.join(5)
            server.close()
            os.unlink(path)
            os.rmdir(d)

    def test_never_replies_is_io_error(self):
        d = tempfile.mkdtemp(prefix="shelf-")
        path = os.path.join(d, "h.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)

        def serve_once():
            conn, _ = server.accept()
            time.sleep(1.0)
            conn.close()

        t = threading.Thread(target=serve_once)
        t.start()
        try:
            with self.assertRaises(HerdrError) as ctx:
                Client(path, timeout=0.3).call("tab.list")
            self.assertEqual(ctx.exception.code, "io")
            self.assertFalse(ctx.exception.definite)
        finally:
            t.join(5)
            server.close()
            os.unlink(path)
            os.rmdir(d)

    def test_non_dict_error_value_becomes_message(self):
        d = tempfile.mkdtemp(prefix="shelf-")
        path = os.path.join(d, "h.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)

        def serve_once():
            conn, _ = server.accept()
            with conn:
                conn.recv(65536)
                conn.sendall(b'{"id": "x", "error": "boom"}\n')

        t = threading.Thread(target=serve_once)
        t.start()
        try:
            with self.assertRaises(HerdrError) as ctx:
                Client(path).call("tab.list")
            self.assertEqual(ctx.exception.code, "error")
            self.assertEqual(ctx.exception.message, "boom")
            self.assertTrue(ctx.exception.definite)
        finally:
            t.join(5)
            server.close()
            os.unlink(path)
            os.rmdir(d)


if __name__ == "__main__":
    unittest.main()
