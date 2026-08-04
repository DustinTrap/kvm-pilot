"""Driver auto-detection (#235): probes, verdict priority, refusal-to-guess.

Every scenario runs a real stdlib socket server on loopback and exercises
``detect_driver`` over genuine HTTP/UDP/TCP — the point of #235 is wire
behavior, so nothing here mocks the transport.
"""

from __future__ import annotations

import json
import socket
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kvm_pilot import detect
from kvm_pilot.config import HostConfig
from kvm_pilot.detect import Detection, detect_driver, resolve_auto
from kvm_pilot.errors import KVMPilotError

# -- scenario servers ------------------------------------------------------


class _Routes:
    """A tiny route-table HTTP server: path -> (status, headers, body-dict|bytes)."""

    def __init__(self, routes):
        self.routes = routes

    def __enter__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence test output
                pass

            def do_GET(self):
                hit = outer.routes.get(self.path)
                if hit is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                status, headers, body = hit
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                # send_response_only: the staged headers must fully control the
                # response (send_response injects Python's own Server header,
                # which would defeat the AMT-banner scenario).
                self.send_response_only(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def port(self):
        return self._httpd.server_address[1]

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)


def _cfg(port, **kw):
    kw.setdefault("scheme", "http")
    kw.setdefault("timeout", 1.0)
    # Point the side-channel ports at closed loopback ports by default so a
    # scenario only answers on the interface it stages.
    kw.setdefault("amt_port", _closed_port())
    kw.setdefault("ipmi_port", _closed_port())
    return HostConfig(host="127.0.0.1", port=port, **kw)


def _closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]  # closed once the socket closes


KVMD_401 = (401, {}, {"ok": False, "result": {"error": "unauthorized"}})


# -- verdicts --------------------------------------------------------------


def test_detects_stock_pikvm_from_kvmd_envelope():
    with _Routes({"/api/info": KVMD_401}) as srv:
        det = detect_driver(_cfg(srv.port))
    assert det.driver == "pikvm"
    assert det.interfaces == ("kvmd",)
    assert any("kvmd JSON envelope" in e for e in det.evidence)


def test_detects_glkvm_via_proprietary_upgrade_endpoint():
    routes = {
        "/api/info": KVMD_401,
        "/api/upgrade/version": (200, {}, {"version": "V1.9.1", "model": "GL-RM1PE"}),
    }
    with _Routes(routes) as srv:
        det = detect_driver(_cfg(srv.port))
    assert det.driver == "glkvm"
    assert any("GL-RM1PE" in e for e in det.evidence)


def test_kvmd_wrapped_404_on_upgrade_endpoint_stays_pikvm():
    # kvmd auth-wraps unknown /api/* paths and answers 404 in its own envelope —
    # that is the stock answer, not a GL tell.
    routes = {
        "/api/info": KVMD_401,
        "/api/upgrade/version": (404, {}, {"ok": False, "result": {}}),
    }
    with _Routes(routes) as srv:
        det = detect_driver(_cfg(srv.port))
    assert det.driver == "pikvm"


def test_detects_redfish_service_root():
    routes = {"/redfish/v1/": (200, {}, {"@odata.id": "/redfish/v1/", "RedfishVersion": "1.6.0"})}
    with _Routes(routes) as srv:
        det = detect_driver(_cfg(srv.port))
    assert det.driver == "redfish"
    assert any("RedfishVersion 1.6.0" in e for e in det.evidence)


def test_detects_amt_from_server_banner():
    banner = {"Server": "Intel(R) Active Management Technology 14.1.79"}
    with _Routes({"/": (303, banner, b"")}) as srv:
        det = detect_driver(_cfg(_closed_port(), amt_port=srv.port))
    assert det.driver == "amt"
    assert any("14.1.79" in e for e in det.evidence)


def test_detects_ipmi_from_asf_pong():
    pong_ready = threading.Event()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    def answer():
        pong_ready.set()
        data, addr = sock.recvfrom(64)
        if data == detect._ASF_PING:
            # RMCP header + ASF pong (type 0x40) with an empty data field.
            sock.sendto(bytes([6, 0, 0xFF, 6, 0, 0, 0x11, 0xBE, 0x40, 1, 0, 0]), addr)

    t = threading.Thread(target=answer, daemon=True)
    t.start()
    pong_ready.wait(1)
    try:
        det = detect_driver(_cfg(_closed_port(), ipmi_port=port))
    finally:
        sock.close()
    assert det.driver == "ipmi"


def test_kvmd_outranks_redfish_when_both_answer():
    # Priority is the interface-selection doctrine: full IP-KVM beats BMC.
    routes = {
        "/api/info": KVMD_401,
        "/redfish/v1/": (200, {}, {"RedfishVersion": "1.6.0"}),
    }
    with _Routes(routes) as srv:
        det = detect_driver(_cfg(srv.port))
    assert det.driver == "pikvm"


def test_plain_web_server_is_not_a_verdict():
    # An HTTP server that is neither kvmd, Redfish, nor AMT must not identify.
    with _Routes({"/": (200, {}, b"<html>hi</html>")}) as srv:
        det = detect_driver(_cfg(srv.port))
    assert det.driver is None


# -- the no-detection refusal (the #235 misdiagnosis) ----------------------


def test_ssh_only_host_refuses_with_inventory():
    # The ssh probe targets the fixed port 22, which a hermetic suite cannot
    # bind — stage its outcome via the memo and assert the refusal message
    # carries the inventory and redirects to the SSH tooling (the exact
    # misdiagnosis #235 was filed about).
    detect._MEMO.clear()
    cfg = _cfg(_closed_port())
    canned = Detection(None, ("kvmd ... -> no answer", "ssh :22 -> SSH-2.0-OpenSSH_9.9"), ("ssh",))
    detect._MEMO[(cfg.host, cfg.port, cfg.scheme, cfg.amt_port, cfg.amt_tls, cfg.ipmi_port)] = canned
    with pytest.raises(KVMPilotError) as exc:
        resolve_auto(cfg)
    msg = str(exc.value)
    assert "refusing to guess" in msg
    assert "SSH-2.0-OpenSSH_9.9" in msg  # the probe inventory is in the error
    assert "ssh-check" in msg  # ... and it points at the right tool


def test_nothing_listening_refuses_with_probe_log():
    detect._MEMO.clear()
    cfg = _cfg(_closed_port())
    with pytest.raises(KVMPilotError) as exc:
        resolve_auto(cfg)
    msg = str(exc.value)
    assert "No supported device API detected" in msg
    assert "--driver" in msg  # tells the operator how to pin


# -- resolve_auto plumbing -------------------------------------------------


def test_resolve_auto_rewrites_driver_and_logs_once(caplog):
    detect._MEMO.clear()
    with _Routes({"/api/info": KVMD_401}) as srv:
        cfg = replace(_cfg(srv.port), driver="auto")
        with caplog.at_level("WARNING", logger="kvm_pilot.detect"):
            out1 = resolve_auto(cfg)
            out2 = resolve_auto(cfg)  # memoized: no second probe, no second log
    assert out1.driver == "pikvm" and out2.driver == "pikvm"
    assert cfg.driver == "auto"  # input is not mutated
    warnings = [r for r in caplog.records if "auto-detected" in r.message]
    assert len(warnings) == 1


def test_make_driver_from_config_resolves_auto(monkeypatch):
    from kvm_pilot.client import PiKVMDriver
    from kvm_pilot.drivers import make_driver_from_config

    seen = {}

    def fake_resolve(cfg):
        seen["cfg"] = cfg
        return replace(cfg, driver="pikvm")

    monkeypatch.setattr("kvm_pilot.detect.resolve_auto", fake_resolve)
    cfg = HostConfig(host="127.0.0.1", driver="auto")
    drv = make_driver_from_config(cfg)
    assert isinstance(drv, PiKVMDriver)
    assert seen["cfg"].driver == "auto"


def test_default_driver_is_auto_everywhere(monkeypatch, tmp_path):
    from kvm_pilot.config import resolve_host

    monkeypatch.delenv("KVM_PILOT_DRIVER", raising=False)
    assert HostConfig(host="h").driver == "auto"
    assert resolve_host(host="h", config_path=tmp_path / "none.toml").driver == "auto"
