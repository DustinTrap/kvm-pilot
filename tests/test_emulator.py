"""End-to-end smoke tests over the REAL transport against a local fake kvmd.

Pure stdlib (see emulator.py); runs on a macOS dev machine and Linux CI with no
Docker and no hardware. Complements the mocked unit tests by exercising the
actual urllib request path, retry/backoff, auth headers, and secret redaction.
"""

from __future__ import annotations

import pytest

from emulator import EmulatorServer
from kvm_pilot import KVMClient
from kvm_pilot.errors import KVMPilotError, SafetyError
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
    assert info["hw"]["platform"] == "fake"
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
    assert info["hw"]["platform"] == "fake"


def test_password_redacted_over_real_transport(emu):
    emu.state.echo_password = True
    with pytest.raises(KVMPilotError) as ei:
        _client(emu).get_info()
    msg = str(ei.value)
    assert "s3cr3t" not in msg
    assert "REDACTED" in msg
