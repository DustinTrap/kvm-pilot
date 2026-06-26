"""Tests for the HTTP transport: retries, TOTP, redaction."""

import io
import json
import urllib.error

import pytest

from kvm_pilot.errors import AuthError, BusyError, ConnectionError
from kvm_pilot.http import HTTP


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _ok_body(result=None):
    return json.dumps({"ok": True, "result": result or {}}).encode()


def test_totp_appended_to_password(monkeypatch):
    h = HTTP("host", "admin", "pw", totp_secret="ABC")
    monkeypatch.setattr("kvm_pilot.http._totp_now", lambda secret: "123456")
    assert h._effective_passwd() == "pw123456"


def test_no_totp_passthrough():
    h = HTTP("host", "admin", "pw")
    assert h._effective_passwd() == "pw"


def test_retries_on_busy_then_succeeds(monkeypatch):
    h = HTTP("host", "admin", "pw", max_retries=2, backoff_base=0.0)
    calls = {"n": 0}

    def fake_urlopen(req, context=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 2:
            raise urllib.error.HTTPError(req.full_url, 409, "busy", {}, io.BytesIO(b"busy"))
        return FakeResp(_ok_body({"done": True}))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = h.get("/api/thing")
    assert result == {"done": True}
    assert calls["n"] == 2


def test_busy_exhausts_retries(monkeypatch):
    h = HTTP("host", "admin", "pw", max_retries=1, backoff_base=0.0)

    def fake_urlopen(req, context=None, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 409, "busy", {}, io.BytesIO(b"busy"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(BusyError):
        h.get("/api/thing")


def test_auth_error_not_retried(monkeypatch):
    h = HTTP("host", "admin", "pw", max_retries=3, backoff_base=0.0)
    calls = {"n": 0}

    def fake_urlopen(req, context=None, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 403, "no", {}, io.BytesIO(b"denied"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(AuthError):
        h.get("/api/thing")
    assert calls["n"] == 1  # auth failures are not retryable


def test_password_redacted_in_error(monkeypatch):
    h = HTTP("host", "admin", "supersecret", max_retries=0)

    def fake_urlopen(req, context=None, timeout=None):
        # Server echoes the password back in the body (worst case).
        raise urllib.error.HTTPError(
            req.full_url, 400, "bad", {}, io.BytesIO(b"bad supersecret value")
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(Exception) as ei:
        h.get("/api/thing")
    assert "supersecret" not in str(ei.value)
    assert "REDACTED" in str(ei.value)


def test_network_error_wrapped(monkeypatch):
    h = HTTP("host", "admin", "pw", max_retries=0)

    def fake_urlopen(req, context=None, timeout=None):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ConnectionError):
        h.get("/api/thing")
