"""End-to-end smoke tests over the REAL transport against a local fake kvmd.

Pure stdlib (see emulator.py); runs on a macOS dev machine and Linux CI with no
Docker and no hardware. Complements the mocked unit tests by exercising the
actual urllib request path, retry/backoff, auth headers, and secret redaction.
"""

from __future__ import annotations

import pytest

from emulator import EmulatorServer
from kvm_pilot import KVMClient
from kvm_pilot.errors import AuthError, KVMPilotError, SafetyError
from kvm_pilot.safety import allow_all, deny_all


@pytest.fixture
def emu():
    with EmulatorServer() as server:
        yield server


def _client(server, **kw) -> KVMClient:
    c = KVMClient(server.host, "admin", "s3cr3t", port=server.port, scheme="http", **kw)
    c._http._backoff_base = 0.0  # no real sleeps between retries
    return c


def test_login_sets_auth_token(emu):
    assert _client(emu).login() == "fake-token-123"


def test_get_info_roundtrip(emu):
    info = _client(emu).get_info()
    assert info["hw"]["platform"]["base"] == "fake"
    assert ("GET", "/api/info") in emu.state.calls


def test_snapshot_returns_jpeg(emu):
    assert _client(emu).snapshot().startswith(b"\xff\xd8")


def test_destructive_power_gated_when_denied(emu):
    with pytest.raises(SafetyError):
        _client(emu, confirm=deny_all).power_off_hard()
    assert ("POST", "/api/atx/power") not in emu.state.calls


def test_power_on_sent_when_allowed(emu):
    _client(emu, confirm=allow_all).power_on()
    assert ("POST", "/api/atx/power") in emu.state.calls
    assert emu.state.powered_on is True


def test_auth_header_carries_credentials(emu):
    _client(emu).get_info()
    assert emu.state.last_headers.get("x-kvmd-user") == "admin"
    assert emu.state.last_headers.get("x-kvmd-passwd") == "s3cr3t"


def test_retry_on_503_then_succeeds(emu):
    emu.state.fail_status = 503
    emu.state.fail_times = 1
    info = _client(emu).get_info()
    assert info["hw"]["platform"]["base"] == "fake"


def test_msd_upload_streams_correct_bytes(emu, tmp_path):
    # Over the real transport: the streamed file arrives intact, with a pinned
    # Content-Length matching the byte count (proving no chunked fallback and no
    # truncation).
    iso = tmp_path / "boot.iso"
    payload = b"\x00\x01\x02\x03" * 5000
    iso.write_bytes(payload)
    _client(emu, confirm=allow_all).msd_upload_file(str(iso))
    assert ("POST", "/api/msd/write") in emu.state.calls
    assert emu.state.last_content_length == len(payload)
    assert emu.state.last_body_len == len(payload)


def test_password_redacted_over_real_transport(emu):
    emu.state.echo_password = True
    with pytest.raises(KVMPilotError) as ei:
        _client(emu).get_info()
    msg = str(ei.value)
    assert "s3cr3t" not in msg
    assert "REDACTED" in msg


def test_get_logs_returns_text(emu):
    # The non-follow log path was previously untested (emulator had no handler).
    text = _client(emu).get_logs()
    assert "kvmd started" in text
    assert ("GET", "/api/log") in emu.state.calls


def test_wrong_password_raises_auth_error(emu):
    # The fake now validates credentials, like real kvmd.
    from kvm_pilot import KVMClient
    c = KVMClient(emu.host, "admin", "wrong-pw", port=emu.port, scheme="http")
    c._http._backoff_base = 0.0
    with pytest.raises(AuthError):
        c.get_info()


def test_unknown_post_route_404s(emu):
    # A typo'd endpoint 404s instead of a lenient 200 — so a driver typo would fail.
    c = _client(emu)
    with pytest.raises(KVMPilotError):
        c._http.post("/api/hid/typodest")


def test_credentials_are_sent_on_state_changing_post(emu):
    _client(emu, confirm=allow_all).power_on()
    assert ("POST", "/api/atx/power") in emu.state.calls
    assert emu.state.last_headers.get("x-kvmd-user") == "admin"
    assert emu.state.last_headers.get("x-kvmd-passwd") == "s3cr3t"


def test_network_guard_blocks_non_loopback():
    import socket
    s = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="Blocked external network"):
            s.connect(("198.51.100.7", 80))  # TEST-NET-2, never dialed
    finally:
        s.close()
