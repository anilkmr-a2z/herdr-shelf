"""Client for herdr's local socket API: one JSON request per line."""

from __future__ import annotations

import itertools
import json
import os
import socket


class HerdrError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class Client:
    def __init__(self, socket_path: str | None = None, timeout: float = 10.0):
        path = socket_path or os.environ.get("HERDR_SOCKET_PATH")
        if not path:
            raise HerdrError("no_socket", "HERDR_SOCKET_PATH is not set")
        self.socket_path = path
        self.timeout = timeout
        self._ids = itertools.count(1)

    def call(self, method: str, params: dict | None = None) -> dict:
        request = {"id": f"shelf:{next(self._ids)}", "method": method, "params": params or {}}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout)
            try:
                s.connect(self.socket_path)
            except OSError as e:
                raise HerdrError("unavailable", str(e)) from e
            s.sendall((json.dumps(request) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        if not buf.strip():
            raise HerdrError("empty_response", method)
        response = json.loads(buf)
        if "error" in response:
            err = response.get("error") or {}
            raise HerdrError(err.get("code", "error"), err.get("message", ""))
        return response.get("result") or {}
