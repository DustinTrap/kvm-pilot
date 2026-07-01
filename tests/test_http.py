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


def test_read_timeout_maps_to_kvm_timeout_error(monkeypatch):
    # urllib does NOT wrap read-phase timeouts in URLError — they must still land
    # in the kvm-pilot taxonomy, not escape as builtins.TimeoutError.
    from kvm_pilot.errors import TimeoutError as KVMTimeoutError

    h = HTTP("host", "admin", "pw", max_retries=3, backoff_base=0.0)
    calls = {"n": 0}

    def fake_urlopen(req, context=None, timeout=None):
        calls["n"] += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(KVMTimeoutError):
        h.get("/api/thing")
    assert calls["n"] == 1  # a timed-out request may have been delivered: no retry


def test_connect_timeout_maps_to_kvm_timeout_error(monkeypatch):
    from kvm_pilot.errors import TimeoutError as KVMTimeoutError

    h = HTTP("host", "admin", "pw", max_retries=0)

    def fake_urlopen(req, context=None, timeout=None):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(KVMTimeoutError):
        h.get("/api/thing")


def test_mid_request_reset_maps_to_connection_error(monkeypatch):
    h = HTTP("host", "admin", "pw", max_retries=0)

    def fake_urlopen(req, context=None, timeout=None):
        raise ConnectionResetError("peer reset")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ConnectionError):
        h.get("/api/thing")


def test_ambiguous_failure_retried_for_get_but_not_post(monkeypatch):
    # A reset after the request was sent is ambiguous: the device may have
    # executed it. GETs retry; a destructive POST must never re-fire.
    h = HTTP("host", "admin", "pw", max_retries=2, backoff_base=0.0)
    calls = {"n": 0}

    def fake_urlopen(req, context=None, timeout=None):
        calls["n"] += 1
        raise ConnectionResetError("peer reset")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ConnectionError):
        h.get("/api/thing")
    assert calls["n"] == 3  # GET: retried to exhaustion

    calls["n"] = 0
    with pytest.raises(ConnectionError):
        h.post("/api/atx/power")
    assert calls["n"] == 1  # POST: never re-fired


def test_connect_refused_still_retried_for_post(monkeypatch):
    # A connect-phase failure (URLError) means the request never reached the
    # device, so even a POST is safe to retry.
    h = HTTP("host", "admin", "pw", max_retries=2, backoff_base=0.0)
    calls = {"n": 0}

    def fake_urlopen(req, context=None, timeout=None):
        calls["n"] += 1
        raise urllib.error.URLError("refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ConnectionError):
        h.post("/api/atx/power")
    assert calls["n"] == 3


def test_ipv6_host_is_bracketed():
    assert HTTP("fd00::10", "u", "p")._base == "https://[fd00::10]:443"
    assert HTTP("192.168.8.1", "u", "p")._base == "https://192.168.8.1:443"


def test_totp_secret_itself_is_redacted(monkeypatch):
    monkeypatch.setattr("kvm_pilot.http._totp_now", lambda secret: "123456")
    h = HTTP("host", "admin", "pw", totp_secret="JBSWY3DPEHPK3PXP", max_retries=0)

    def fake_urlopen(req, context=None, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 400, "bad", {}, io.BytesIO(b"echo JBSWY3DPEHPK3PXP end")
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(Exception) as ei:
        h.get("/api/thing")
    assert "JBSWY3DPEHPK3PXP" not in str(ei.value)


def test_effective_password_with_totp_redacted_in_error(monkeypatch):
    monkeypatch.setattr("kvm_pilot.http._totp_now", lambda secret: "123456")
    h = HTTP("host", "admin", "supersecret", totp_secret="ABC", max_retries=0)

    def fake_urlopen(req, context=None, timeout=None):
        # Worst case: the device reflects the full X-KVMD-Passwd header (pw+TOTP).
        raise urllib.error.HTTPError(
            req.full_url, 400, "bad", {}, io.BytesIO(b"got supersecret123456 back")
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(Exception) as ei:
        h.get("/api/thing")
    msg = str(ei.value)
    assert "supersecret123456" not in msg
    assert "123456" not in msg  # the live TOTP code must be redacted too
    assert "REDACTED" in msg


def test_default_tls_is_unverified_with_one_time_warning(caplog):
    import logging
    import ssl

    import kvm_pilot.http as http_mod

    http_mod._unverified_warned = False  # reset the once-per-process latch
    with caplog.at_level(logging.WARNING, logger="kvm_pilot.http"):
        h1 = HTTP("host", "u", "p")
        h2 = HTTP("host2", "u", "p")
    assert h1._ssl_ctx.verify_mode == ssl.CERT_NONE
    assert h2._ssl_ctx.verify_mode == ssl.CERT_NONE
    warnings = [r for r in caplog.records if "TLS verification is DISABLED" in r.message]
    assert len(warnings) == 1  # loud, but once per process


def test_verify_ssl_true_verifies_and_does_not_warn(caplog):
    import logging
    import ssl

    import kvm_pilot.http as http_mod

    http_mod._unverified_warned = False
    with caplog.at_level(logging.WARNING, logger="kvm_pilot.http"):
        h = HTTP("host", "u", "p", verify_ssl=True)
    assert h._ssl_ctx.verify_mode == ssl.CERT_REQUIRED
    assert h._ssl_ctx.check_hostname is True
    assert not [r for r in caplog.records if "TLS" in r.message]


def test_ssl_ca_file_pins_and_wins_over_verify_ssl(monkeypatch):
    import ssl

    captured = {}
    real = ssl.create_default_context

    def fake_create(cafile=None, **kw):
        captured["cafile"] = cafile
        return real()  # a verifying context; loading the fake path is bypassed

    monkeypatch.setattr("kvm_pilot.http.ssl.create_default_context", fake_create)
    h = HTTP("host", "u", "p", verify_ssl=False, ssl_ca_file="/pki/device.pem")
    assert captured["cafile"] == "/pki/device.pem"
    assert h._ssl_ctx.verify_mode == ssl.CERT_REQUIRED  # pinning implies verification
