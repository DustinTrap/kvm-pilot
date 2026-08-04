"""Driver auto-detection: probe the host, pick the driver that speaks its language.

``--driver`` / ``KVM_PILOT_DRIVER`` / the profile ``driver`` key used to fall
back to a **silent ``pikvm`` assumption** — any address fed to the CLI was
treated as a PiKVM, and a wrong guess produced an authoritative-looking
``CRITICAL api-reachable`` healthcheck ("PiKVM API unreachable") against a host
that was never a PiKVM in the first place (#235). The fallback is now ``auto``:
a short pass of cheap, read-only probes over every supported interface class,
run at driver-build time and memoized per process.

Probe order = verdict priority (full IP-KVM first, then the BMC classes — the
richest capable interface wins, matching the interface-selection doctrine):

==========  ========================================  ===========================
verdict     probe                                     marker
==========  ========================================  ===========================
pikvm       ``GET /api/info`` on the API port         kvmd-style JSON (``ok`` key)
glkvm       ``GET /api/upgrade/version`` (authed)     GL's proprietary endpoint
redfish     ``GET /redfish/v1/``                      ``RedfishVersion`` / odata
amt         ``GET /`` on 16992 (or 16993 TLS)         Intel AMT ``Server`` banner
ipmi        RMCP/ASF presence ping (UDP 623)          ASF pong
==========  ========================================  ===========================

The first marker that matches decides; the remaining probes are skipped (a real
PiKVM costs one request, like the old default did). Only when *nothing* matches
is the full inventory — including a TCP/banner probe of SSH on :22 — assembled,
so the failure can say "this host answers SSH only — not a KVM/BMC" instead of
the misleading CRITICAL that motivated #235. SSH can never win the verdict: it
identifies a reachable OS, not a device API.

``glkvm`` vs stock ``pikvm``: GL firmware self-reports as a Raspberry Pi PiKVM
in ``/api/info`` (#126), so the only tell is GL's proprietary
``/api/upgrade/version`` — and kvmd auth-wraps unknown ``/api/*`` paths, so the
refine probe must send credentials to tell GL's 200 from stock's 404. When the
credentials don't authenticate the verdict stays ``pikvm``; the healthcheck's
``driver-identity`` check re-fingerprints with working credentials later.
"""

from __future__ import annotations

import json
import logging
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .errors import KVMPilotError

if TYPE_CHECKING:
    from .config import HostConfig

logger = logging.getLogger(__name__)

# Per-probe network timeout. Detection must stay cheap even against a
# fully-filtered host (worst case: every probe times out), so the config's
# request timeout (default 30s) is capped hard.
_TIMEOUT_CAP = 2.0

# RMCP/ASF presence ping (IPMI 2.0 spec §13.2.3): RMCP header (version 6,
# reserved, seq 0xFF = no ack, class 6 = ASF) + ASF header (IANA 4542,
# type 0x80 = presence ping, tag 1, reserved, zero data length).
_ASF_PING = bytes([0x06, 0x00, 0xFF, 0x06, 0x00, 0x00, 0x11, 0xBE, 0x80, 0x01, 0x00, 0x00])
_ASF_PONG_TYPE = 0x40


@dataclass(frozen=True)
class Detection:
    """Outcome of one probe pass: the winning driver kind (or ``None``), the
    ordered human-readable probe log, and every interface that answered."""

    driver: str | None
    evidence: tuple[str, ...]
    interfaces: tuple[str, ...]


def _http_probe(
    url: str, *, timeout: float, headers: dict[str, str] | None = None
) -> tuple[int, dict | None, str] | None:
    """``GET url`` -> ``(status, parsed-JSON-dict-or-None, Server header)``, or
    ``None`` when nothing HTTP answered. Detection probes are identification
    reads against an operator-supplied management address — no retries, no
    redirects followed off-host, tiny body cap, TLS unverified (device certs are
    self-signed; the verdict never carries secrets back)."""
    ctx = None
    if url.startswith("https"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # nosec B323 - identification probe only
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # nosec B310
            status, body, server = resp.status, resp.read(4096), resp.headers.get("Server", "")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096)
        except OSError:
            body = b""
        status, server = exc.code, exc.headers.get("Server", "") if exc.headers else ""
    except Exception:  # noqa: BLE001 - refused/timeout/TLS/DNS all mean "no answer"
        return None
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    return status, parsed if isinstance(parsed, dict) else None, server


def _probe_kvmd(base: str, timeout: float) -> tuple[bool, str]:
    """kvmd (PiKVM family) marker: any HTTP status whose body is kvmd's
    ``{"ok": ..., "result": ...}`` envelope — an unauthenticated ``/api/info``
    401s but still answers in that shape."""
    got = _http_probe(f"{base}/api/info", timeout=timeout)
    if got is None:
        return False, f"kvmd {base}/api/info -> no answer"
    status, parsed, _ = got
    if parsed is not None and "ok" in parsed:
        return True, f"kvmd {base}/api/info -> HTTP {status} (kvmd JSON envelope)"
    return False, f"kvmd {base}/api/info -> HTTP {status} (not a kvmd response)"


def _refine_gl(base: str, cfg: HostConfig, timeout: float) -> tuple[bool, str]:
    """GL fork tell: the proprietary ``/api/upgrade/version`` exists only on GL
    firmware, but kvmd auth-wraps unknown paths — send credentials so stock
    kvmd's 404 is distinguishable from GL's 200."""
    got = _http_probe(
        f"{base}/api/upgrade/version",
        timeout=timeout,
        headers={"X-KVMD-User": cfg.user, "X-KVMD-Passwd": cfg.passwd},
    )
    if got is None:
        return False, "glkvm /api/upgrade/version -> no answer"
    status, parsed, _ = got
    body = parsed or {}
    if set(body) == {"ok", "result"} and isinstance(body.get("result"), dict):
        body = body["result"]
    if status == 200 and (body.get("version") or body.get("model")):
        ident = ", ".join(str(v) for v in (body.get("model"), body.get("version")) if v)
        return True, f"glkvm /api/upgrade/version -> HTTP 200 ({ident})"
    return False, f"glkvm /api/upgrade/version -> HTTP {status} (stock kvmd answer)"


def _probe_redfish(base: str, timeout: float) -> tuple[bool, str]:
    """DMTF Redfish marker: the service root is unauthenticated by spec."""
    got = _http_probe(f"{base}/redfish/v1/", timeout=timeout)
    if got is None:
        return False, f"redfish {base}/redfish/v1/ -> no answer"
    status, parsed, _ = got
    body = parsed or {}
    if status == 200 and ("RedfishVersion" in body or any(k.startswith("@odata") for k in body)):
        ver = body.get("RedfishVersion", "unversioned")
        return True, f"redfish {base}/redfish/v1/ -> HTTP 200 (RedfishVersion {ver})"
    return False, f"redfish {base}/redfish/v1/ -> HTTP {status} (not a Redfish service root)"


def _probe_amt(cfg: HostConfig, timeout: float) -> tuple[bool, str]:
    """Intel AMT marker: the ME's embedded web server on 16992 (16993 TLS)
    names itself in the ``Server`` header, even on the 401 digest challenge."""
    port = cfg.amt_port or 16992
    candidates = [(f"https://{cfg.host}:{port}", port)] if cfg.amt_tls else [
        (f"http://{cfg.host}:{port}", port),
        (f"https://{cfg.host}:16993", 16993),
    ]
    tried = []
    for base, p in candidates:
        got = _http_probe(f"{base}/", timeout=timeout)
        if got is None:
            tried.append(f":{p} no answer")
            continue
        status, _, server = got
        if "intel" in server.lower() and "management" in server.lower():
            return True, f"amt :{p} -> HTTP {status} (Server: {server})"
        tried.append(f":{p} HTTP {status} (Server: {server or 'none'} - not AMT)")
    return False, f"amt {'; '.join(tried)}"


def _probe_ipmi(cfg: HostConfig, timeout: float) -> tuple[bool, str]:
    """IPMI marker: RMCP/ASF presence ping — the one IPMI exchange that needs
    no credentials or session. UDP, so silence just means 'no' (or filtered)."""
    port = cfg.ipmi_port or 623
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(_ASF_PING, (cfg.host, port))
            data, _ = sock.recvfrom(64)
    except Exception:  # noqa: BLE001 - timeout/refused/unresolvable = no answer
        return False, f"ipmi udp :{port} -> no ASF pong"
    if len(data) >= 9 and data[8] == _ASF_PONG_TYPE:
        return True, f"ipmi udp :{port} -> ASF presence pong"
    return False, f"ipmi udp :{port} -> non-ASF reply"


def _probe_ssh(host: str, timeout: float) -> tuple[bool, str]:
    """Inventory-only: an SSH banner on :22 identifies a reachable OS (never a
    verdict — it says nothing about which, if any, device API exists)."""
    try:
        with socket.create_connection((host, 22), timeout=timeout) as sock:
            sock.settimeout(timeout)
            banner = sock.recv(64).decode("ascii", "replace").strip()
    except Exception:  # noqa: BLE001
        return False, "ssh :22 -> no answer"
    if banner.startswith("SSH-"):
        return True, f"ssh :22 -> {banner}"
    return True, "ssh :22 -> open (no SSH banner)"


def detect_driver(cfg: HostConfig, *, timeout: float | None = None) -> Detection:
    """Run the probe pass for ``cfg.host`` and return the :class:`Detection`.

    Read-only and credential-light: only the GL refine sends the configured
    credentials (kvmd auth-wraps the tell endpoint); everything else is
    unauthenticated. First marker wins and short-circuits the rest.
    """
    t = min(timeout if timeout is not None else cfg.timeout, _TIMEOUT_CAP)
    base = f"{cfg.scheme}://{cfg.host}:{cfg.port}"
    evidence: list[str] = []
    interfaces: list[str] = []

    hit, note = _probe_kvmd(base, t)
    evidence.append(note)
    if hit:
        interfaces.append("kvmd")
        gl, gl_note = _refine_gl(base, cfg, t)
        evidence.append(gl_note)
        return Detection("glkvm" if gl else "pikvm", tuple(evidence), tuple(interfaces))

    hit, note = _probe_redfish(base, t)
    evidence.append(note)
    if hit:
        interfaces.append("redfish")
        return Detection("redfish", tuple(evidence), tuple(interfaces))

    hit, note = _probe_amt(cfg, t)
    evidence.append(note)
    if hit:
        interfaces.append("amt")
        return Detection("amt", tuple(evidence), tuple(interfaces))

    hit, note = _probe_ipmi(cfg, t)
    evidence.append(note)
    if hit:
        interfaces.append("ipmi")
        return Detection("ipmi", tuple(evidence), tuple(interfaces))

    ssh_up, note = _probe_ssh(cfg.host, t)
    evidence.append(note)
    if ssh_up:
        interfaces.append("ssh")
    return Detection(None, tuple(evidence), tuple(interfaces))


# One probe pass per (host, API port/scheme, AMT/IPMI ports) per process: the
# MCP server rebuilds the driver on every tool call and must not re-probe (or
# re-log) each time.
_MEMO: dict[tuple, Detection] = {}


def resolve_auto(cfg: HostConfig) -> HostConfig:
    """Resolve ``driver = "auto"`` into a concrete driver kind by probing.

    Returns a copy of ``cfg`` with ``driver`` set to the verdict, logging one
    WARNING naming the choice and its evidence (an assumption the operator did
    not spell out must be visible, #235). Raises :class:`KVMPilotError` with the
    full probe inventory when nothing identifies — refusing to guess is the
    point; the old silent ``pikvm`` guess is exactly what produced misleading
    healthchecks.
    """
    key = (cfg.host, cfg.port, cfg.scheme, cfg.amt_port, cfg.amt_tls, cfg.ipmi_port)
    det = _MEMO.get(key)
    if det is None:
        det = detect_driver(cfg)
        _MEMO[key] = det
        if det.driver is not None:
            logger.warning(
                "driver auto-detected: %s for %s (%s) — pin --driver / KVM_PILOT_DRIVER / "
                "the profile 'driver' key to skip probing",
                det.driver, cfg.host, "; ".join(det.evidence),
            )
    if det.driver is not None:
        return replace(cfg, driver=det.driver)

    lines = "\n".join(f"  - {e}" for e in det.evidence)
    ssh_hint = (
        "\nThe host answers on SSH — that identifies a reachable OS, not a KVM/BMC. "
        "If it is the managed host, set ssh_host and use ssh-check / ssh-exec; "
        "kvm-pilot's driver commands need a device API."
        if "ssh" in det.interfaces else ""
    )
    raise KVMPilotError(
        f"No supported device API detected on {cfg.host} — refusing to guess a driver "
        f"(a silent 'pikvm' assumption here is what produced misleading healthchecks, #235).\n"
        f"Probed:\n{lines}{ssh_hint}\n"
        "If the device is temporarily down or its API is disabled, pin the driver "
        "explicitly: --driver <kind>, KVM_PILOT_DRIVER, or the profile's 'driver' key."
    )
