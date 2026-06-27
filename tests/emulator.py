"""
A pure-stdlib fake PiKVM / kvmd REST server for hardware-free smoke tests.

Starts on 127.0.0.1 in a background thread (macOS + Linux, no Docker, no
third-party deps) and answers the endpoints ``KVMClient`` actually calls,
wrapping JSON in the ``{"ok": true, "result": ...}`` envelope. Inject transient
failures or credential echoing via the shared ``FakeKVMState`` to exercise
retry/backoff and secret redaction over the real transport.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Minimal JPEG: SOI + APP0 header, enough for snapshot()/JPEG-sniffing tests.
_FAKE_JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000101000000")


class FakeKVMState:
    """Mutable knobs + captured requests, shared across handler instances."""

    def __init__(self) -> None:
        self.powered_on = False
        self.fail_status: int | None = None
        self.fail_times = 0
        self.echo_password = False
        self.api_disabled = False  # GL firmware: every /api/* returns 404
        self.last_headers: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # silence test output
        pass

    @property
    def _state(self) -> FakeKVMState:
        return self.server.state  # type: ignore[attr-defined]

    def _send(
        self,
        data: bytes,
        status: int = 200,
        ctype: str = "application/json",
        cookie: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, result: object, ok: bool = True, status: int = 200) -> None:
        self._send(json.dumps({"ok": ok, "result": result}).encode(), status=status)

    def _pre(self) -> bool:
        """Record the call and apply injected echo/failures. True = already answered."""
        st = self._state
        # Header names are case-insensitive on the wire; normalize to lowercase.
        st.last_headers = {k.lower(): v for k, v in self.headers.items()}
        path = self.path.split("?", 1)[0]
        st.calls.append((self.command, path))
        if self.command != "GET":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
        if st.echo_password:
            passwd = self.headers.get("X-KVMD-Passwd", "")
            self._send(f"bad request for {passwd}".encode(), status=400, ctype="text/plain")
            return True
        if st.fail_times > 0 and st.fail_status:
            st.fail_times -= 1
            self._send(b"transient", status=st.fail_status, ctype="text/plain")
            return True
        if st.api_disabled and path.startswith("/api/"):
            # GL.iNet firmware blocks the API at nginx -> 404 (HTML in reality).
            self._send(b"<html>404</html>", status=404, ctype="text/html")
            return True
        return False

    def do_GET(self) -> None:
        if self._pre():
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/auth/check":
            self._json({})
        elif path == "/api/info":
            self._json({
                "hw": {"platform": {"base": "fake", "model": "GL-RM1PE"}},
                "system": {"kvmd": {"version": "4.2-gl-test"}},
            })
        elif path == "/api/atx":
            self._json({"leds": {"power": self._state.powered_on}})
        elif path == "/api/streamer/snapshot":
            self._send(_FAKE_JPEG, ctype="image/jpeg")
        else:
            self._json({}, ok=False, status=404)

    def do_POST(self) -> None:
        if self._pre():
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/auth/login":
            self._send(b'{"ok": true}', cookie="auth_token=fake-token-123; Path=/")
        elif path == "/api/atx/power":
            self._state.powered_on = True
            self._json({})
        else:
            self._json({})  # generic OK for hid / msd / etc.


class EmulatorServer:
    """Context manager that runs the fake kvmd on an ephemeral localhost port."""

    def __init__(self) -> None:
        self.state = FakeKVMState()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.state = self.state  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return self._httpd.server_address[0]

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def __enter__(self) -> EmulatorServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)
