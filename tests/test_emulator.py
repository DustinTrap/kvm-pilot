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


def test_set_jiggler_toggles_and_reads_back(emu):
    # #159 keep-awake: set_params?jiggler=1/0 flips device state; the returned
    # jiggler dict reflects it, so the CLI can report ON/off truthfully.
    c = _client(emu)
    assert c.set_jiggler(True) == {"active": True, "enabled": True, "interval": 20}
    assert emu.state.jiggler_active is True
    assert ("POST", "/api/hid/set_params") in emu.state.calls
    assert c.set_jiggler(False)["active"] is False
    assert emu.state.jiggler_active is False


def test_recover_hid_resets_and_confirms_reattach(emu):
    # #160: recover_hid resets the gadget, then confirms it reaches the target.
    c = _client(emu)
    assert c.recover_hid() is True  # default emulator reports connected
    assert ("POST", "/api/hid/reset") in emu.state.calls


def test_recover_hid_returns_false_when_gadget_stays_unreachable(emu):
    emu.state.hid_connected = False
    assert _client(emu).recover_hid(timeout=0.4) is False


def test_display_awake_restores_prior_jiggler_over_transport(emu):
    # #161 A1: the context manager enables the jiggler for the block and restores
    # the prior device state on exit — verified over the real HTTP transport.
    c = _client(emu)
    assert emu.state.jiggler_active is False
    with c.display_awake():
        assert emu.state.jiggler_active is True
    assert emu.state.jiggler_active is False


def test_retry_on_503_then_succeeds(emu):
    emu.state.fail_status = 503
    emu.state.fail_times = 1
    info = _client(emu).get_info()
    assert info["hw"]["platform"]["base"] == "fake"


def test_mount_iso_verifies_media_online(emu, tmp_path):
    iso = tmp_path / "boot.iso"
    iso.write_bytes(b"\x00" * 1024)
    name = _client(emu, confirm=allow_all).mount_iso(str(iso))
    assert name == "boot.iso"
    assert emu.state.msd["online"] is True


def test_mount_iso_raises_when_media_stays_offline(emu, tmp_path):
    # Regression (#77): GLKVM accepts set_params/set_connected while the host
    # sees no device (online stays false) — e.g. the GL-side MSD toggle is off.
    # The mount must fail loudly instead of reporting success.
    from kvm_pilot.errors import MediaOfflineError

    emu.state.msd_stays_offline = True
    iso = tmp_path / "boot.iso"
    iso.write_bytes(b"\x00" * 1024)
    with pytest.raises(MediaOfflineError) as ei:
        _client(emu, confirm=allow_all).mount_iso(str(iso), verify_timeout=0.6)
    assert "online=false" in str(ei.value)


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


def test_truncated_json_surfaces_typed_protocol_error(emu):
    # #170 over the real transport: a JSON endpoint answering with a truncated
    # body (still labeled application/json) must raise the typed error, not
    # leak raw bytes into a dict-expecting caller as an AttributeError.
    from kvm_pilot.errors import ProtocolError

    emu.state.garbage_json_once = True
    c = _client(emu)
    with pytest.raises(ProtocolError) as ei:
        c.get_info()
    assert "non-JSON" in str(ei.value)
    c.close()
