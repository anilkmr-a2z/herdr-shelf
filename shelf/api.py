"""Client for herdr's local socket API: one JSON request per line."""

from __future__ import annotations

import itertools
import json
import os
import socket


class HerdrError(Exception):
    """code identifies what went wrong; definite tells apart "herdr refused"
    (a connect failure, or an error object herdr actually sent back) from
    "outcome unknown" (a transport error or bad reply after we made contact,
    where the requested operation may or may not have taken effect).
    """

    def __init__(self, code: str, message: str = "", definite: bool = False):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.definite = definite


class Client:
    def __init__(self, socket_path: str | None = None, timeout: float = 10.0):
        path = socket_path or os.environ.get("HERDR_SOCKET_PATH")
        if not path:
            raise HerdrError("no_socket", "HERDR_SOCKET_PATH is not set", definite=True)
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
                raise HerdrError("unavailable", str(e), definite=True) from e
            try:
                s.sendall((json.dumps(request) + "\n").encode())
                chunks = []
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if chunk.endswith(b"\n"):
                        break
            except OSError as e:
                raise HerdrError("io", str(e), definite=False) from e
        buf = b"".join(chunks)
        if not buf.strip():
            raise HerdrError("empty_response", method, definite=False)
        try:
            response = json.loads(buf)
        except ValueError as e:
            raise HerdrError("bad_response", str(e), definite=False) from e
        if not isinstance(response, dict):
            raise HerdrError("bad_response", "reply was not a JSON object", definite=False)
        if response.get("error"):
            err = response["error"] or {}
            raise HerdrError(err.get("code", "error"), err.get("message", ""), definite=True)
        return response.get("result") or {}
