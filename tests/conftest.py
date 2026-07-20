"""Shared test fixtures and fakes."""

from __future__ import annotations

import socket
from typing import Any

import pytest

from kvm_pilot.client import KVMClient

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


@pytest.fixture(autouse=True)
def _no_external_network(monkeypatch):
    """Fail any test that tries to open a non-loopback socket.

    The suite is supposed to be hermetic — the in-process emulators bind to
    127.0.0.1 and everything else mocks the transport. This turns an accidental
    real-network call (a driver reaching out, a leaked URL) into a loud failure
    instead of a slow, flaky, or data-leaking test.
    """
    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOOPBACK:
            raise RuntimeError(f"Blocked external network connect to {host!r} in tests")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def _isolated_health_cache(tmp_path, monkeypatch):
    """Redirect the healthcheck cache to a per-test tmp file (never touch ~/.cache),
    and clear the per-process first-connection audit guard so tests stay independent."""
    monkeypatch.setenv("KVM_PILOT_HEALTH_CACHE", str(tmp_path / "health-cache.json"))
    # Isolate the firmware-registry cache/override so tests never read a real
    # ~/.cache copy (loader falls back to the bundled registry).
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("KVM_PILOT_FIRMWARE_DB", raising=False)
    # ... and the run-ledger override, so evidence tests read the bundled copy.
    monkeypatch.delenv("KVM_PILOT_TEST_LEDGER", raising=False)
    from kvm_pilot.health import reset_session_audit

    reset_session_audit()
    # Standing approvals (#192) are module-level, per-process — drop any a test
    # left behind so grants never leak across tests.
    from kvm_pilot.mcp.act import revoke_standing_grants

    revoke_standing_grants()


class FakeHTTP:
    """Records requests and returns canned results instead of hitting a network."""

    def __init__(self, results: dict[str, Any] | None = None):
        self.calls: list[dict] = []
        self.results = results or {}
        self._auth_token = None
        self._user = "admin"
        self._passwd = "secret"
        self._base = "https://fake:443"

    def _effective_passwd(self) -> str:
        return self._passwd

    def _record(self, method, path, **kw):
        self.calls.append({"method": method, "path": path, **kw})
        return self.results.get(path, {})

    def get(self, path, params=None, **kw):
        return self._record("GET", path, params=params, **kw)

    def post(self, path, params=None, body=None, content_type=None, **kw):
        return self._record("POST", path, params=params, body=body, **kw)

    def request(self, method, path, **kw):
        return self._record(method, path, **kw)

    def login(self):
        self._auth_token = "tok"
        return "tok"

    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]


@pytest.fixture
def fake_http():
    return FakeHTTP()


@pytest.fixture
def client(fake_http):
    c = KVMClient("fake", "admin", "secret")
    c._http = fake_http
    return c


@pytest.fixture
def emu():
    """A running pure-stdlib fake DMTF Redfish service (see redfish_emulator.py).

    Shared by the RedfishDriver tests and the CLI ``--driver redfish`` tests.
    """
    from redfish_emulator import RedfishEmulator

    with RedfishEmulator() as e:
        yield e


@pytest.fixture
def amt_emu():
    """A running pure-stdlib fake Intel AMT WS-Man service (see amt_emulator.py)."""
    from amt_emulator import AmtEmulator

    with AmtEmulator() as e:
        yield e


@pytest.fixture
def amt_rfb():
    """A running pure-stdlib fake AMT KVM-redirection (RFB/VNC) server."""
    from amt_rfb_emulator import AmtRfbEmulator

    with AmtRfbEmulator() as e:
        yield e
