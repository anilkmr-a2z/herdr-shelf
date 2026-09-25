"""A stand-in for herdr's local socket, for tests.

handlers maps a method name to a callable(params) that returns the result dict,
or raises FakeError(code, message) to send an error response. Every request is
recorded in calls as (method, params).
"""

import json
import os
import socket
import tempfile
import threading


class FakeError(Exception):
    def __init__(self, code, message=""):
        super().__init__(code)
        self.code = code
        self.message = message


class FakeHerdr:
    def __init__(self, handlers=None):
        self.handlers = dict(handlers or {})
        self.calls = []
        self._dir = tempfile.mkdtemp(prefix="shelf-")
        self.path = os.path.join(self._dir, "h.sock")
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(8)
        self._server.settimeout(0.1)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                if not buf:
                    continue
                req = json.loads(buf)
                params = req.get("params") or {}
                self.calls.append((req["method"], params))
                handler = self.handlers.get(req["method"])
                if handler is None:
                    resp = {"id": req["id"], "error": {"code": "unknown_method", "message": req["method"]}}
                else:
                    try:
                        resp = {"id": req["id"], "result": handler(params)}
                    except FakeError as e:
                        resp = {"id": req["id"], "error": {"code": e.code, "message": e.message}}
                conn.sendall((json.dumps(resp) + "\n").encode())

    def methods(self):
        return [m for m, _ in self.calls]

    def close(self):
        self._stop = True
        self._thread.join(2)
        self._server.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        os.rmdir(self._dir)
