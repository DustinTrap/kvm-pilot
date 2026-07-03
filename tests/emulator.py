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

# The POST routes KVMClient actually drives. A real kvmd 404s anything else, so
# a typo'd endpoint in the driver would slip through a lenient fake.
_VALID_POST_PATHS = frozenset({
    "/api/auth/login", "/api/auth/logout",
    "/api/atx/power", "/api/atx/click",
    "/api/hid/print", "/api/hid/reset", "/api/hid/set_params",
    "/api/hid/events/send_key", "/api/hid/events/send_shortcut",
    "/api/hid/events/send_mouse_move", "/api/hid/events/send_mouse_relative",
    "/api/hid/events/send_mouse_button", "/api/hid/events/send_mouse_wheel",
    "/api/msd/write", "/api/msd/write_remote", "/api/msd/set_params",
    "/api/msd/set_connected", "/api/msd/remove", "/api/msd/reset",
    "/api/gpio/switch", "/api/gpio/pulse",
    "/api/upgrade/start", "/api/upgrade/upload",
})


class FakeKVMState:
    """Mutable knobs + captured requests, shared across handler instances."""

    def __init__(self) -> None:
        self.powered_on = False
        self.fail_status: int | None = None
        self.fail_times = 0
        self.echo_password = False
        self.api_disabled = False  # GL firmware: every /api/* returns 404
        # Healthcheck knobs: ATX wiring, GPIO outputs, MSD state.
        self.atx_enabled = True
        self.gpio_outputs: dict[str, object] = {}
        self.msd: dict[str, object] = {"online": False, "drive": {"image": None}}
        # GL /api/upgrade/* remote-firmware surface. Absent by default (older
        # firmware / fallback path) so get_firmware_info keeps returning the base
        # kvmd version; a test flips upgrade_present to exercise the flash path.
        self.upgrade_present = False
        self.upgrade_enabled = True
        self.upgrade_version: dict[str, object] = {
            "model": "GL-RM1PE", "version": "V1.5.1 release2"}
        self.upgrade_image_size = 307581578
        # Real kvmd 401s /api/* without valid X-KVMD-User/Passwd (or auth_token
        # cookie). Enforced by default so a dropped-credential regression fails.
        self.expected_user = "admin"
        self.expected_passwd = "s3cr3t"
        self.auth_token = "fake-token-123"
        self.last_headers: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        # Last POST body: declared Content-Length vs bytes actually received.
        self.last_content_length: int | None = None
        self.last_body_len: int | None = None


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
            st.last_content_length = length
            st.last_body_len = len(self.rfile.read(length)) if length else 0
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
        # Auth: every /api/* needs matching X-KVMD credentials or a valid
        # auth_token cookie. Login authenticates by body creds, so it's exempt.
        if path.startswith("/api/") and path != "/api/auth/login" and not self._authed():
            self._send(b"forbidden", status=403, ctype="text/plain")
            return True
        return False

    def _authed(self) -> bool:
        st = self._state
        if (self.headers.get("X-KVMD-User") == st.expected_user
                and self.headers.get("X-KVMD-Passwd") == st.expected_passwd):
            return True
        cookie = self.headers.get("Cookie", "")
        return f"auth_token={st.auth_token}" in cookie

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
            self._json({
                "enabled": self._state.atx_enabled,
                "leds": {"power": self._state.powered_on},
            })
        elif path == "/api/gpio":
            self._json({"state": {"outputs": self._state.gpio_outputs}})
        elif path == "/api/msd":
            self._json(self._state.msd)
        elif path == "/api/streamer":
            self._json({"source": {"online": True, "resolution": {"width": 1920, "height": 1080}}})
        elif path == "/api/streamer/snapshot":
            self._send(_FAKE_JPEG, ctype="image/jpeg")
        elif path == "/api/log":
            # kvmd streams plain-text journal lines (non-follow returns the buffer).
            self._send(b"[boot] kvmd started\n[atx] power on\n", ctype="text/plain")
        elif path.startswith("/api/upgrade/") and self._state.upgrade_present:
            # GL's proprietary remote-firmware surface (read-only endpoints).
            if path == "/api/upgrade/status":
                self._json({"enabled": self._state.upgrade_enabled})
            elif path == "/api/upgrade/version":
                self._json(self._state.upgrade_version)
            elif path == "/api/upgrade/download":
                self._json({"size": self._state.upgrade_image_size})
            else:
                self._json({}, ok=False, status=404)
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
        elif path in _VALID_POST_PATHS:
            self._json({})  # generic OK for a real hid/msd/gpio route
        else:
            self._json({}, ok=False, status=404)  # a typo'd route 404s, like kvmd


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
