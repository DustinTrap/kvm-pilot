"""
Device preflight healthcheck (issue #80).

Audits a KVM device's **readiness/recovery**, **security posture**, and
**firmware currency** before it is trusted for real work, then gates
destructive/multi-step operations on the result.

Design (see issue #80):

* Every check returns a :class:`CheckResult` with a :class:`Severity`. A check
  that does not apply to the active driver returns ``None`` and is skipped — a
  driver never has to hand-maintain a check list, exactly like
  ``known_quirks()``.
* Severity is tiered: ``CRITICAL`` blocks a destructive op until it is
  explicitly overridden; ``WARNING``/``INFO`` inform and proceed. In an
  interactive run the operator is prompted (continue/abort); unattended, a
  critical **fails closed** unless a stored acknowledgement exists.
* Results are split into **stable posture** (``cacheable=True`` — firmware,
  TLS, wiring/recovery-path; changes only via config/firmware) and **volatile
  readiness** (``cacheable=False`` — media online, video/HID liveness; flips at
  runtime). The cache accelerates the stable audit; volatile checks are always
  re-probed live at point-of-use so a stale "OK" can never mask a runtime
  change (the failure mode this whole feature exists to prevent).

The module is stdlib-only and imports nothing from the drivers — it probes a
driver purely through its public, **read-only** methods (never a
``DESTRUCTIVE_OPS`` call), so it is safe to run on a live host.
"""

from __future__ import annotations

import enum
import json
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import KVMPilotError

# Version compare + known-bad range matching live in firmware_registry (shared with reconcile).
from .firmware_registry import _affected, _vercmp
from .safety import ConfirmCallback

__all__ = [
    "Severity",
    "Pillar",
    "AutoFix",
    "CheckResult",
    "HealthReport",
    "HealthGateError",
    "run_healthcheck",
    "enforce_gate",
    "preflight",
    "preflight_once",
    "reset_session_audit",
    "HealthCache",
    "CHECKS",
]


class Severity(enum.IntEnum):
    """Ordered so ``max(...)`` yields the worst result."""

    OK = 0
    INFO = 1
    WARNING = 2
    CRITICAL = 3

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


class Pillar(enum.StrEnum):
    READINESS = "readiness"
    SECURITY = "security"
    FIRMWARE = "firmware"


class HealthGateError(KVMPilotError):
    """Raised when a destructive op is blocked by an unacknowledged CRITICAL."""


@dataclass(frozen=True)
class AutoFix:
    """An opt-in remediation the operator can choose to apply.

    ``apply(driver)`` performs the fix. Only ``safe_reversible`` fixes are ever
    applied automatically (with per-item consent); anything else is
    report-only. A fix must never perturb a running guest.
    """

    description: str
    safe_reversible: bool
    apply: Callable[[Any], None]


@dataclass(frozen=True)
class CheckResult:
    id: str
    pillar: Pillar
    severity: Severity
    title: str
    detail: str
    remediation: str = ""
    # Stable posture is cacheable; volatile readiness must be re-probed live.
    cacheable: bool = True
    auto_fix: AutoFix | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pillar": str(self.pillar),
            "severity": str(self.severity),
            "title": self.title,
            "detail": self.detail,
            "remediation": self.remediation,
            "cacheable": self.cacheable,
            "auto_fix": self.auto_fix.description if self.auto_fix else None,
        }


@dataclass
class HealthReport:
    host: str
    driver_kind: str
    firmware: str | None
    results: list[CheckResult] = field(default_factory=list)
    ran_at: float = 0.0

    @property
    def worst(self) -> Severity:
        return max((r.severity for r in self.results), default=Severity.OK)

    @property
    def criticals(self) -> list[CheckResult]:
        return [r for r in self.results if r.severity is Severity.CRITICAL]

    @property
    def cache_key(self) -> str:
        return f"{self.driver_kind}@{self.host}#{self.firmware or '?'}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "driver": self.driver_kind,
            "firmware": self.firmware,
            "ran_at": self.ran_at,
            "worst": str(self.worst),
            "results": [r.to_dict() for r in self.results],
        }


# --------------------------------------------------------------------------- #
# Check helpers                                                               #
# --------------------------------------------------------------------------- #

# A check probes a driver and returns a result, or None if it does not apply.
Check = Callable[[Any], "CheckResult | None"]

_KNOWN_DEFAULT_CREDS = {("admin", "admin"), ("admin", "password"), ("root", "root")}


def _driver_kind(driver: Any) -> str:
    name = type(driver).__name__.lower()
    for kind in ("glkvm", "blikvm", "redfish", "ipmi", "amt", "fake"):
        if kind in name:
            return kind
    return "pikvm"


def _firmware_of(driver: Any) -> str | None:
    fn = getattr(driver, "get_firmware_info", None)
    if fn is None:
        return None
    try:
        return fn().get("version")
    except KVMPilotError:
        return None


def _result(result: Any) -> dict[str, Any]:
    """Unwrap a kvmd ``{"ok": ..., "result": {...}}`` envelope if present."""
    if isinstance(result, dict) and "result" in result and isinstance(result["result"], dict):
        return result["result"]
    return result if isinstance(result, dict) else {}


# --------------------------------------------------------------------------- #
# Readiness / recovery checks (mostly volatile)                              #
# --------------------------------------------------------------------------- #


def check_api_reachable(driver: Any) -> CheckResult | None:
    """The device answers and authenticates. Volatile: re-probe live."""
    get_info = getattr(driver, "get_info", None)
    if get_info is None:
        return None
    try:
        get_info()
    except KVMPilotError as exc:
        return CheckResult(
            id="api-reachable",
            pillar=Pillar.READINESS,
            severity=Severity.CRITICAL,
            title="Device API reachable",
            detail=f"{type(exc).__name__}: {exc}",
            remediation="Check host/credentials/network; for GLKVM enable the REST API "
            "in /etc/kvmd/nginx-kvmd.conf.",
            cacheable=False,
        )
    return CheckResult(
        id="api-reachable",
        pillar=Pillar.READINESS,
        severity=Severity.OK,
        title="Device API reachable",
        detail="Authenticated and responding.",
        cacheable=False,
    )


def check_driver_identity(driver: Any) -> CheckResult | None:
    """Wrong-driver fingerprint (#145): a plain-PiKVM profile pointed at a GL unit.

    GL firmware self-reports as a stock Raspberry Pi PiKVM in ``/api/info``
    (#126), so the only cheap tell is GL's proprietary ``/api/upgrade/version``.
    Probe it only when the profile chose the plain ``pikvm`` driver — the forks
    already know who they are, and a 404 is the expected stock answer.
    """
    from .client import PiKVMDriver

    if type(driver) is not PiKVMDriver:
        return None
    try:
        up = driver._http.get("/api/upgrade/version")
    except KVMPilotError:
        return None  # 404 = stock PiKVM, the expected answer
    if not (isinstance(up, dict) and (up.get("version") or up.get("model"))):
        return None
    ident = ", ".join(str(v) for v in (up.get("model"), up.get("version")) if v)
    return CheckResult(
        id="driver-identity",
        pillar=Pillar.READINESS,
        severity=Severity.WARNING,
        title="Wrong driver? Device looks like a GL.iNet GLKVM",
        detail=(
            f"This device answers GL's proprietary /api/upgrade/version ({ident}) "
            "but the profile uses the plain 'pikvm' driver — GL quirks, the "
            "API-disabled hint, dual-version firmware reporting, and the gated "
            "flash capability are all inactive."
        ),
        remediation='Set driver = "glkvm" in the profile (or pass --driver glkvm).',
    )


def check_ssh_reachable(driver: Any) -> CheckResult | None:
    """The managed host's OS answers on its SSH port. Volatile; self-skips.

    Complements ``recovery-path``: an in-band SSH channel to the target OS is a
    remote-recovery lever (#81). Only present when the profile configured
    ``ssh_host`` — the driver factory attaches ``driver.ssh_channel`` then, and
    this check self-skips otherwise. A host that is down is **INFO, not a
    warning**: powered-off / pre-network / mid-install hosts normally don't
    answer, and that must not inflate the report or gate a destructive op.
    """
    channel = getattr(driver, "ssh_channel", None)
    if channel is None:
        return None  # SSH-to-target not configured for this profile
    try:
        up = channel.ssh_reachable()
    except Exception:  # noqa: BLE001 - a liveness probe must never break the audit
        up = False
    if up:
        return CheckResult(
            id="ssh-reachable",
            pillar=Pillar.READINESS,
            severity=Severity.OK,
            title="Host SSH reachable",
            detail=f"{channel.target}:{channel.port} accepts TCP — in-band recovery available.",
            cacheable=False,
        )
    return CheckResult(
        id="ssh-reachable",
        pillar=Pillar.READINESS,
        severity=Severity.INFO,
        title="Host SSH reachable",
        detail=(
            f"{channel.target}:{channel.port} did not answer — the OS may be off, "
            "pre-network, or firewalled."
        ),
        cacheable=False,
    )


# The remote-first recovery ladder (#222) — the single code copy of the
# doctrine's ordering. Interpolated wherever a failure should carry the next
# step; the full playbook is the skill's references/recovery.md, re-served by
# the MCP `doctrine` tool (topic 'recovery').
RECOVERY_ORDER = (
    "Wake-on-LAN -> in-band SSH -> KVM-side recovery (recover-hid / "
    "appliance reboot) -> Intel AMT -> physical"
)


def check_recovery_path(driver: Any) -> CheckResult | None:
    """Is there ANY out-of-band reset if the guest hangs? (Stable posture.)

    The highest-value check: a hung guest with no OOB reset can be bricked/
    stranded when the KVM is remote. ATX being *enabled in kvmd* is not enough —
    it must be wired to the host (GL rigs frequently are not).
    """
    supports = getattr(driver, "supports", None)
    get_atx = getattr(driver, "get_atx_state", None)

    if get_atx is None:
        # No ATX surface (e.g. Redfish BMC or the fake driver). If the driver
        # advertises POWER, its reset is genuine out-of-band (BMC/emulator).
        try:
            from .drivers.base import Capability

            has_power = bool(supports and supports(Capability.POWER))
        except Exception:  # pragma: no cover - defensive
            has_power = False
        if has_power:
            return CheckResult(
                id="recovery-path",
                pillar=Pillar.READINESS,
                severity=Severity.OK,
                title="Out-of-band recovery path",
                detail="Driver exposes out-of-band power/reset.",
            )
        return None

    # PiKVM family: ATX must actually be wired to the host header.
    atx_wired = False
    try:
        atx = _result(get_atx())
        atx_wired = bool(atx.get("enabled"))
    except KVMPilotError:
        atx_wired = False

    gpio_power = False
    get_gpio = getattr(driver, "get_gpio_state", None)
    if get_gpio is not None:
        try:
            outputs = _result(get_gpio()).get("state", {}).get("outputs", {})
            gpio_power = bool(outputs)
        except KVMPilotError:
            gpio_power = False

    if atx_wired or gpio_power:
        how = "ATX" if atx_wired else "GPIO"
        return CheckResult(
            id="recovery-path",
            pillar=Pillar.READINESS,
            severity=Severity.OK,
            title="Out-of-band recovery path",
            detail=f"{how} power/reset control is wired.",
        )
    return CheckResult(
        id="recovery-path",
        pillar=Pillar.READINESS,
        severity=Severity.CRITICAL,
        title="Out-of-band recovery path",
        detail="No out-of-band reset: ATX reports enabled=false and no GPIO power "
        "channels are defined. A hung guest cannot be recovered remotely.",
        remediation="Wire the ATX cable to the host front-panel power/reset header, "
        "or provision a GPIO/Redfish/IPMI reset path. Until one exists, if the "
        f"guest hangs the remote-first recovery order is: {RECOVERY_ORDER} "
        "(full playbook: the MCP `doctrine` tool, topic 'recovery').",
    )


def access_paths(driver: Any) -> dict:
    """The lockout-exposure view: which INDEPENDENT recovery paths are live for a
    device (rock-solid access, #162). Each path is labeled by its failure
    *domain*, so ``independent_domains`` never oversells redundancy when several
    paths ride the same appliance — the only truly hardware-independent domain is
    out-of-band power; every in-band path dies with the appliance or its network.
    """
    paths: list[dict] = []

    get_info = getattr(driver, "get_info", None)
    rest_live: bool | None = None
    if get_info is not None:
        try:
            get_info()
            rest_live = True
        except KVMPilotError:
            rest_live = False
    paths.append({
        "path": "kvmd-rest", "domain": "appliance", "kind": "primary",
        "configured": get_info is not None, "live": rest_live,
        "detail": "The kvmd REST API — degrades exactly when the box gets sick.",
    })

    chan = getattr(driver, "appliance_channel", None)
    paths.append({
        "path": "appliance-ssh", "domain": "appliance", "kind": "in-band-independent",
        "configured": chan is not None,
        "live": chan.ssh_reachable() if chan is not None else None,
        "detail": ("SSH to the KVM's own OS (independent daemon on :22); observes/"
                   "recovers the encoder wedge REST can't."
                   if chan is not None else "Not configured (set appliance_ssh)."),
    })

    tchan = getattr(driver, "ssh_channel", None)
    paths.append({
        "path": "target-ssh", "domain": "target-network", "kind": "in-band",
        "configured": tchan is not None,
        "live": tchan.ssh_reachable() if tchan is not None else None,
        "detail": ("SSH to the managed OS itself; recover from inside when KVM "
                   "video/HID is flaky."
                   if tchan is not None else "Not configured (set ssh_host)."),
    })

    rec = check_recovery_path(driver)
    oob_live = rec is not None and rec.severity is Severity.OK
    paths.append({
        "path": "oob-power", "domain": "out-of-band", "kind": "out-of-band",
        "configured": oob_live, "live": oob_live if rec is not None else None,
        "detail": (rec.detail if rec is not None else "unknown")
        + " The ONLY path that survives a fully hung appliance/target.",
    })

    hid_fn = getattr(driver, "get_hid_state", None)
    hid_live: bool | None = None
    if hid_fn is not None:
        try:
            hid_live = bool(_result(hid_fn()).get("connected"))
        except KVMPilotError:
            hid_live = False
    paths.append({
        "path": "console-hid", "domain": "appliance", "kind": "in-band",
        "configured": hid_fn is not None, "live": hid_live,
        "detail": "Ctrl+Alt+Del over the emulated keyboard (only if the OS honors it).",
    })

    live = [p for p in paths if p["live"]]
    return {
        "paths": paths,
        "summary": {
            "live_count": len(live),
            "independent_domains": len({p["domain"] for p in live}),
            "out_of_band_live": any(p["path"] == "oob-power" and p["live"] for p in paths),
        },
    }


def check_video_signal(driver: Any) -> CheckResult | None:
    """Live video from the host. Volatile."""
    fn = getattr(driver, "has_video_signal", None)
    if fn is None:
        return None
    try:
        alive = bool(fn())
    except KVMPilotError:
        alive = False
    # Resolution/online/format detail turns "video looks wrong" from a guess
    # into a diagnosis (#143) — no-signal vs bad-format vs subsystem-down.
    detail_fn = getattr(driver, "video_signal_info", None)
    sig = ""
    info: dict = {}
    if detail_fn is not None:
        try:
            info = detail_fn()
            sig = ", ".join(f"{k}={v}" for k, v in info.items() if v is not None)
        except KVMPilotError:
            info = {}
    # On-demand streamer idle (block is null): has_video_signal's optimistic
    # fallback would read as "signal live" here, so report it honestly as
    # unconfirmed instead — a snapshot would start the streamer and reveal the
    # truth. Prevents a false-confident "live" on a possibly-dark target (#165).
    if info.get("streamer_offline"):
        return CheckResult(
            id="video-signal",
            pillar=Pillar.READINESS,
            severity=Severity.INFO,
            title="Video signal",
            detail="Capture subsystem idle (on-demand streamer not started) — signal "
            "unconfirmed. On GL the encoder starts only for an active video client, "
            "and a snapshot does NOT start it (#173); open the WebRTC stream/web "
            "console or use the trigger-then-wait recovery (#142) to get a frame.",
            cacheable=False,
        )
    if alive:
        return CheckResult(
            id="video-signal",
            pillar=Pillar.READINESS,
            severity=Severity.OK,
            title="Video signal",
            detail="Capture stream is live" + (f" ({sig})." if sig else "."),
            cacheable=False,
        )
    # No signal: distinguish "display asleep (target on)" from "target off/cable".
    # If the HID gadget is attached the target is enumerating USB, so it is
    # powered — the display just DPMS-slept, which is the real root of the
    # #126/#142 "snapshot fails though video works" reports. keep-awake (the
    # jiggler) wakes and holds it (#161).
    target_on = False
    hid_fn = getattr(driver, "get_hid_state", None)
    if hid_fn is not None:
        try:
            target_on = bool(_result(hid_fn()).get("connected"))
        except KVMPilotError:
            target_on = False
    if target_on and hasattr(driver, "set_jiggler"):
        return CheckResult(
            id="video-signal",
            pillar=Pillar.READINESS,
            severity=Severity.WARNING,
            title="Display asleep (target on)",
            detail="No video signal, but the emulated HID is attached — the target "
            "is powered and its display has DPMS-slept" + (f" ({sig})." if sig else "."),
            remediation="Wake and hold the display with `kvm-pilot keep-awake on` "
            "(kvmd's jiggler), or disable display sleep on the target OS.",
            cacheable=False,
            auto_fix=AutoFix(
                description="Enable the keep-awake jiggler (a benign mouse nudge) "
                "to wake and hold the display.",
                safe_reversible=True,
                apply=lambda d: d.set_jiggler(True),
            ),
        )
    return CheckResult(
        id="video-signal",
        pillar=Pillar.READINESS,
        severity=Severity.WARNING,
        title="Video signal",
        detail="No video signal from the host" + (f" ({sig})." if sig else "."),
        remediation="Host may be powered off or the HDMI capture is disconnected.",
        cacheable=False,
    )


def check_hid_reachable(driver: Any) -> CheckResult | None:
    """Does the KVM's emulated keyboard/mouse actually reach the target? Volatile.

    kvmd presents the keyboard/mouse as a USB gadget over its OTG/device port; the
    gadget must attach to a USB *host* port on the target. A charge-only/damaged
    cable or a wrong-port-role connection leaves the gadget unattached — kvmd then
    generates HID reports that fail ``write select`` (no connected host) and every
    keystroke/click is silently dropped, which reads as "the target ignores input"
    or is misread as "target powered off" (#155). Pairs with ``video-signal`` to
    disambiguate: HID-reachable + no-signal = target on, display asleep;
    HID-not-reachable = a USB OTG cable/port fault.
    """
    fn = getattr(driver, "get_hid_state", None)
    if fn is None:
        return None
    try:
        hid = _result(fn())
    except KVMPilotError:
        return None
    connected = hid.get("connected")
    if connected is None:
        return None  # firmware doesn't report it -> nothing to assert
    kbd = _result(hid.get("keyboard")).get("online")
    mouse = _result(hid.get("mouse")).get("online")
    if connected:
        return CheckResult(
            id="hid-reachable",
            pillar=Pillar.READINESS,
            severity=Severity.OK,
            title="HID reaches the target",
            detail=f"Emulated keyboard/mouse online (keyboard={kbd}, mouse={mouse}).",
            cacheable=False,
        )
    # A gadget reset re-enumerates the USB HID and can clear a soft write-select
    # wedge (#160) — reversible, never touches guest power. A physical cable/port
    # fault won't recover, so this is a best-effort AutoFix, not a guarantee.
    fix = None
    if hasattr(driver, "recover_hid"):
        fix = AutoFix(
            description="Reset and re-enumerate the USB HID gadget.",
            safe_reversible=True,
            apply=lambda d: d.recover_hid(),
        )
    return CheckResult(
        id="hid-reachable",
        pillar=Pillar.READINESS,
        severity=Severity.WARNING,
        title="HID reaches the target",
        detail=(
            f"The KVM's emulated keyboard/mouse is not reaching the target "
            f"(connected=false, keyboard.online={kbd}, mouse.online={mouse}) — "
            "keystrokes and clicks are generated but not delivered."
        ),
        remediation=(
            "Check the USB OTG/HID cable: it must be data-capable (not charge-only) "
            "and in a USB host port on the target. A persistent "
            "'HID … write select' failure in `logs` confirms the gadget can't "
            "attach; transient flapping during a target reboot is benign."
        ),
        cacheable=False,
        auto_fix=fix,
    )


# The GL RV1126 hard-loop signature: kvmd repeatedly failing to (re)initialize
# the hardware video encoder. Functional and REST-visible (the log survives the
# wedge), unlike loadavg — which sits at ~10 on these units even when perfectly
# idle/healthy (measured 2026-07-07), so loadavg must NOT be used as the tell.
_ENCODER_WEDGE_PATTERNS = (
    "init rv1126 encoder failed",
    "resolution failed",
    "init rv1126 vpss failed",
)
# A genuine wedge hard-loops (many failures/sec); a couple of lines is a transient
# reinit blip a unit recovered from (seen live: a working unit with 2 "resolution
# failed" lines). Require a real loop before warning.
_ENCODER_WEDGE_MIN_HITS = 3


def check_encoder_wedge(driver: Any) -> CheckResult | None:
    """GL RV1126 video-encoder wedge: kvmd hard-loops on encoder (re)init and the
    JPEG snapshot path 503s or returns non-image bytes. Detected functionally from
    the kvmd log (which survives the wedge); recovered only by an appliance reboot
    (the wedged threads are unkillable kernel threads — a service restart can't
    clear them). Self-skips on drivers without logs / when no failures are seen.
    Volatile.
    """
    get_logs = getattr(driver, "get_logs", None)
    if get_logs is None:
        return None
    try:
        log = get_logs().lower()
    except KVMPilotError:
        return None
    hits = sum(log.count(p) for p in _ENCODER_WEDGE_PATTERNS)
    if hits < _ENCODER_WEDGE_MIN_HITS:
        return None  # healthy, or a recovered transient blip -> nothing to assert
    # Appliance-channel context if configured (loadavg is context, NOT the tell).
    context = ""
    chan = getattr(driver, "appliance_channel", None)
    if chan is not None:
        try:
            la = chan.loadavg()
            if la is not None:
                context = f"; appliance load {la}"
        except Exception:  # noqa: BLE001 - diagnostics must never fail the check
            context = ""
    can_reboot = chan is not None and hasattr(chan, "reboot")
    remediation = (
        "Reboot the KVM appliance to reinitialize the encoder — "
        + (
            "`kvm-pilot appliance reboot`. "
            if can_reboot
            else "no appliance-reboot path is configured (set appliance_ssh); reboot "
            "it manually. "
        )
        + "A service restart cannot clear it (the wedged threads are kernel threads). "
        "The durable fix is upstream firmware / capping capture ≤1080p (#107/#151)."
    )
    return CheckResult(
        id="encoder-wedge",
        pillar=Pillar.READINESS,
        severity=Severity.WARNING,
        title="Video encoder wedged",
        detail=f"The kvmd log shows {hits} RV1126 encoder (re)init failure(s){context} — "
        "the JPEG snapshot path will 503 or hand back non-image bytes.",
        remediation=remediation,
        cacheable=False,
    )


def _reconnect_media(driver: Any) -> None:  # pragma: no cover - exercised via AutoFix test
    driver.msd_disconnect()
    time.sleep(0.5)
    driver.msd_connect()


def check_msd_online(driver: Any) -> CheckResult | None:
    """Virtual media attached but not actually presented to the host. Volatile."""
    fn = getattr(driver, "get_msd_state", None)
    if fn is None:
        return None
    try:
        msd = _result(fn())
    except KVMPilotError:
        return None
    drive = msd.get("drive") or {}
    image = drive.get("image")
    online = msd.get("online")
    # Substring of the device name the host shows for this brand's MSD gadget
    # (#78); None on drivers where it has not been observed on real hardware.
    pattern = getattr(driver, "virtual_media_host_pattern", None)
    if not image:
        return CheckResult(
            id="msd-online",
            pillar=Pillar.READINESS,
            severity=Severity.OK,
            title="Virtual media",
            detail="No image attached.",
            cacheable=False,
        )
    if online:
        detail = "Image attached and online (presented to host)."
        if pattern:
            detail += (
                f" The host should now list a '{pattern}' device (e.g. in its "
                "boot menu); a bare generic CD/DVD entry instead means the "
                "medium is not really presented (#78)."
            )
        return CheckResult(
            id="msd-online",
            pillar=Pillar.READINESS,
            severity=Severity.OK,
            title="Virtual media",
            detail=detail,
            cacheable=False,
        )
    fix = None
    if hasattr(driver, "msd_disconnect") and hasattr(driver, "msd_connect"):
        fix = AutoFix(
            description="Re-select and reconnect the virtual media gadget.",
            safe_reversible=True,
            apply=_reconnect_media,
        )
    return CheckResult(
        id="msd-online",
        pillar=Pillar.READINESS,
        severity=Severity.WARNING,
        title="Virtual media",
        detail="An image is attached but online=false — it is not presented to the "
        "host, so a boot-from-media will fail."
        + (f" The host's boot menu will show no '{pattern}' device." if pattern else ""),
        remediation="Enable virtual media on the device (GL firmware disables the "
        "USB gadget separately), then reconnect.",
        cacheable=False,
        auto_fix=fix,
    )


# --------------------------------------------------------------------------- #
# Security-posture checks (stable)                                           #
# --------------------------------------------------------------------------- #


def check_tls_posture(driver: Any) -> CheckResult | None:
    http = getattr(driver, "_http", None)
    if http is None:
        return None
    verify = getattr(http, "_verify_ssl", False)
    ca = getattr(http, "_ssl_ca_file", None)
    if verify or ca:
        return CheckResult(
            id="tls-posture",
            pillar=Pillar.SECURITY,
            severity=Severity.OK,
            title="TLS verification",
            detail="Certificate verification is enabled/pinned.",
        )
    return CheckResult(
        id="tls-posture",
        pillar=Pillar.SECURITY,
        severity=Severity.WARNING,
        title="TLS verification",
        detail="TLS verification is disabled — credentials travel over an "
        "unauthenticated channel (MITM-able).",
        remediation="Pin the device certificate with ssl_ca_file / --ssl-ca-file, "
        "or enable verify_ssl.",
    )


def check_default_creds(driver: Any) -> CheckResult | None:
    http = getattr(driver, "_http", None)
    if http is None:
        return None
    user = getattr(http, "_user", None)
    passwd = getattr(http, "_passwd", None)
    if (user, passwd) in _KNOWN_DEFAULT_CREDS:
        return CheckResult(
            id="default-creds",
            pillar=Pillar.SECURITY,
            severity=Severity.WARNING,
            title="Default credentials",
            detail=f"Authenticating as {user!r} with a well-known default password.",
            remediation="Change the device password from its factory default.",
        )
    return CheckResult(
        id="default-creds",
        pillar=Pillar.SECURITY,
        severity=Severity.OK,
        title="Default credentials",
        detail="Not using a known default password.",
    )


def check_exposed_services(driver: Any) -> CheckResult | None:
    get_info = getattr(driver, "get_info", None)
    if get_info is None:
        return None
    try:
        info = _result(get_info())
    except KVMPilotError:
        return None
    extras = info.get("extras")
    if not isinstance(extras, dict):
        return None
    remote = {"vnc", "ipmi"}
    enabled = sorted(
        name
        for name, meta in extras.items()
        if isinstance(meta, dict) and meta.get("enabled")
    )
    exposed = [name for name in enabled if name in remote]
    if exposed:
        return CheckResult(
            id="exposed-services",
            pillar=Pillar.SECURITY,
            severity=Severity.WARNING,
            title="Exposed services",
            detail=f"Broad-access services enabled: {', '.join(exposed)}.",
            remediation="Disable unused remote-access services (VNC/IPMI) or "
            "restrict them to the management network.",
        )
    return CheckResult(
        id="exposed-services",
        pillar=Pillar.SECURITY,
        severity=Severity.INFO,
        title="Exposed services",
        detail=f"Enabled extras: {', '.join(enabled) or 'none'}.",
    )


# --------------------------------------------------------------------------- #
# Intel AMT / vPro posture                                                   #
# --------------------------------------------------------------------------- #
#
# AMT-specific checks: transport TLS (plaintext 16992 by default), provisioning /
# control mode, the SOL/KVM redirection-listener state, the user-consent posture,
# and the 8-char RFB password. They share ONE memoized ``driver.amt_health()``
# read so the audit doesn't flood AMT's WS-Man endpoint (it rate-limits bursts),
# and self-skip on every non-AMT driver (no ``amt_health`` method).


def check_amt_transport_security(driver: Any) -> CheckResult | None:
    health = getattr(driver, "amt_health", None)
    if health is None:
        return None
    if health().get("tls"):
        return CheckResult(
            id="amt-transport", pillar=Pillar.SECURITY, severity=Severity.OK,
            title="AMT transport security", detail="WS-Man over TLS (16993).")
    return CheckResult(
        id="amt-transport", pillar=Pillar.SECURITY, severity=Severity.WARNING,
        title="AMT transport security",
        detail="WS-Man is plaintext HTTP on 16992 — HTTP Digest protects the password hash, "
               "but all management traffic (and the SOL/KVM setup) travels unencrypted.",
        remediation="Provision AMT for TLS and set amt_tls=true (port 16993), or keep the ME "
                    "on an isolated management VLAN.")


def check_amt_provisioning(driver: Any) -> CheckResult | None:
    health = getattr(driver, "amt_health", None)
    if health is None:
        return None
    h = health()
    state, mode = h.get("provisioning_state"), h.get("control_mode")
    if state is not None and state != "post":
        return CheckResult(
            id="amt-provisioning", pillar=Pillar.READINESS, severity=Severity.CRITICAL,
            title="AMT provisioning",
            detail=f"AMT is not provisioned (state={state!r}); the ME will not answer "
                   "management requests.",
            remediation="Provision AMT (MEBx or host-based configuration) before use.",
            cacheable=False)
    modes = {"acm": "Admin Control Mode (consent-off allowed)",
             "ccm": "Client Control Mode (KVM user-consent is mandatory)"}
    return CheckResult(
        id="amt-provisioning", pillar=Pillar.READINESS, severity=Severity.OK,
        title="AMT provisioning",
        detail=f"Provisioned; {modes.get(mode, 'control mode unknown')}.", cacheable=False)


def check_amt_redirection(driver: Any) -> CheckResult | None:
    health = getattr(driver, "amt_health", None)
    if health is None:
        return None
    h = health()
    off = [name for name, on in (("SOL/IDE-R (16994)", h.get("sol_listener")),
                                 ("KVM (5900)", h.get("kvm_5900"))) if on is False]
    if not off:
        return CheckResult(
            id="amt-redirection", pillar=Pillar.READINESS, severity=Severity.OK,
            title="AMT redirection listeners",
            detail="SOL and KVM redirection listeners are on.", cacheable=False)
    return CheckResult(
        id="amt-redirection", pillar=Pillar.READINESS, severity=Severity.WARNING,
        title="AMT redirection listeners",
        detail=f"Listener off: {', '.join(off)} — console/snapshot won't work until enabled.",
        remediation="Run `kvm-pilot amt enable-sol` and/or `enable-kvm` (opens the listeners "
                    "over WS-Man; no MEBx trip).",
        cacheable=False)


def check_amt_kvm_consent(driver: Any) -> CheckResult | None:
    health = getattr(driver, "amt_health", None)
    if health is None:
        return None
    h = health()
    if h.get("kvm_5900") is not True:
        return None  # KVM listener off -> consent posture is moot
    if h.get("kvm_consent_required") is False:
        return CheckResult(
            id="amt-kvm-consent", pillar=Pillar.SECURITY, severity=Severity.WARNING,
            title="AMT KVM user-consent",
            detail="KVM redirection is enabled with user-consent OFF: anyone with the AMT "
                   "credentials can view and control the screen with no on-screen prompt.",
            remediation="Re-enable consent with `kvm-pilot amt enable-kvm` (drop --no-consent) "
                        "unless silent access is intended for this box.")
    return CheckResult(
        id="amt-kvm-consent", pillar=Pillar.SECURITY, severity=Severity.OK,
        title="AMT KVM user-consent", detail="KVM sessions require on-screen user consent.")


def check_amt_rfb_password(driver: Any) -> CheckResult | None:
    health = getattr(driver, "amt_health", None)
    if health is None:
        return None
    h = health()
    # Only a readiness problem once KVM is actually enabled: enable_kvm/snapshot
    # need the AMT-required exactly-8-char password. Otherwise stay quiet.
    if h.get("kvm_5900") is not True or h.get("rfb_password_ok"):
        return None
    return CheckResult(
        id="amt-rfb-password", pillar=Pillar.READINESS, severity=Severity.WARNING,
        title="AMT KVM RFB password",
        detail="KVM 5900 is enabled but amt_kvm_password is not the AMT-required exactly-8-char "
               "complex password — snapshot/HID auth will fail.",
        remediation="Set amt_kvm_password (KVM_PILOT_AMT_KVM_PASSWORD) to an 8-char password "
                    "with an upper, lower, digit and special character.",
        cacheable=False)


# --------------------------------------------------------------------------- #
# Firmware checks (stable)                                                   #
# --------------------------------------------------------------------------- #


def check_firmware_report(driver: Any) -> CheckResult | None:
    fn = getattr(driver, "get_firmware_info", None)
    if fn is None:
        return None
    try:
        fw = fn()
    except KVMPilotError:
        return None
    version = fw.get("version")
    model = fw.get("model")
    kvmd = fw.get("kvmd_version")
    # `version` is the product firmware the UI shows (e.g. GL "V1.9.1 release1");
    # note the kvmd component version too when it differs.
    kvmd_note = f" (kvmd {kvmd})" if kvmd and kvmd != version else ""
    return CheckResult(
        id="firmware-report",
        pillar=Pillar.FIRMWARE,
        severity=Severity.INFO,
        title="Firmware",
        detail=f"model={model or '?'} firmware={version or '?'}{kvmd_note}.",
    )


def check_firmware_quirks(driver: Any) -> CheckResult | None:
    fn = getattr(driver, "known_quirks", None)
    if fn is None:
        return None
    try:
        quirks = fn()
    except KVMPilotError:
        return None
    if not quirks:
        return None
    lines = "; ".join(f"{q.id} ({q.source})" for q in quirks)
    remediation = " | ".join(q.workaround for q in quirks)
    return CheckResult(
        id="firmware-quirks",
        pillar=Pillar.FIRMWARE,
        severity=Severity.WARNING,
        title="Known firmware quirks",
        detail=f"{len(quirks)} quirk(s) apply to this firmware: {lines}.",
        remediation=remediation,
    )


# -- firmware currency (registry-backed) ------------------------------------- #
#
# The registry is the single source of truth for "is this firmware current /
# known-bad" (see docs/firmware-registry.md). It ships bundled (offline default);
# entries are contributed via the firmware-report ingestion pipeline. A device is
# identified by the (vendor, product, version) its driver's get_firmware_info()
# normalizes — so one generic mechanism serves every family (PiKVM, GLKVM, Redfish
# iDRAC/iLO/XCC, and future IPMI/AMT drivers): the vendor-specific bit is only
# "how do I read the version off this box", which lives in the driver.


_REGISTRY_CACHE: dict[str, Any] | None = None


def _load_firmware_registry() -> dict[str, Any]:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        from .firmware_registry import load_registry

        _REGISTRY_CACHE = load_registry()
    return _REGISTRY_CACHE


def _match_firmware(entries: list[dict], vendor: str, product: str) -> dict | None:
    """First registry entry whose vendor equals and whose product is a substring
    of the device's reported product (so ``RV1126B`` matches the messy board
    string, and ``iDRAC9`` matches exactly)."""
    pl = product.lower()
    for e in entries:
        if e.get("vendor", "").strip().lower() == vendor and e.get("product", "").strip().lower() in pl:
            return e
    return None


def _firmware_remediation(entry: dict, source: str) -> str:
    """Remediation text for a stale-firmware finding.

    When the registry entry's ``profile.remote_update`` says this model can be
    flashed over the network, offer the actionable ``firmware-update`` command with
    its risk up front; otherwise fall back to the vendor download pointer. The
    healthcheck never flashes — it only surfaces the option (see docs/firmware-update.md).
    """
    ru = (entry.get("profile") or {}).get("remote_update") or {}
    if not ru.get("supported"):
        return f"Update the firmware. Source: {source}."
    risk = (ru.get("risk") or "unknown").upper()
    recovery = " A failed flash needs physical access to recover." if ru.get("recovery_required") else ""
    return (
        f"Update the firmware. This model supports remote update: run "
        f"`kvm-pilot firmware-update` (RISK: {risk} — review its assessment before "
        f"proceeding).{recovery} Or flash via the vendor UI: {source}."
    )


def check_firmware_currency(driver: Any) -> CheckResult | None:
    """Flag known-bad firmware or an available update, via the firmware registry."""
    fn = getattr(driver, "get_firmware_info", None)
    if fn is None:
        return None
    try:
        fw = fn()
    except KVMPilotError:
        return None
    vendor = (fw.get("vendor") or "").strip().lower()
    product = fw.get("product") or ""
    version = fw.get("version")
    if not (vendor and product and version):
        return None
    entry = _match_firmware(_load_firmware_registry().get("firmware", []), vendor, product)
    if entry is None:
        return None

    # 1) Known-bad: the installed version falls in an affected range.
    for bad in entry.get("known_bad", []):
        if _affected(bad.get("affected", ""), version):
            fixed = f" Fixed in {bad['fixed_in']}." if bad.get("fixed_in") else ""
            return CheckResult(
                id="firmware-currency",
                pillar=Pillar.FIRMWARE,
                severity=Severity.CRITICAL if bad.get("severity") == "critical" else Severity.WARNING,
                title="Known-bad firmware",
                detail=f"{product} {version} matches a known-bad range ({bad['affected']}): {bad['issue']}.{fixed}",
                remediation=_firmware_remediation(entry, bad.get("source", "n/a")),
            )

    # 2) Out of date: strictly behind the latest known release.
    latest = entry.get("latest")
    if latest and _vercmp(version, latest) < 0:
        return CheckResult(
            id="firmware-currency",
            pillar=Pillar.FIRMWARE,
            severity=Severity.WARNING,
            title="Firmware update available",
            detail=f"{product} is on {version}; latest known is {latest} (as of {entry.get('date', '?')}).",
            remediation=_firmware_remediation(entry, entry.get("source", "the vendor download page")),
        )
    return None  # current (or ahead) -> the pillar stays quiet


def check_capability_profile(driver: Any) -> CheckResult | None:
    """Report the stored capability / expected-UX profile for this device.

    These are the differentiators a live probe can't safely determine (absolute
    vs relative mouse, whether virtual media actually presents to the host,
    whether power readings are truthful, the video ceiling). INFO when the profile
    is all-good; WARNING when any axis is degraded, since those directly shape
    what the operator can expect (and whether it's safe to automate).
    """
    fn = getattr(driver, "get_firmware_info", None)
    if fn is None:
        return None
    try:
        fw = fn()
    except KVMPilotError:
        return None
    vendor = (fw.get("vendor") or "").strip().lower()
    product = fw.get("product") or ""
    if not (vendor and product):
        return None
    entry = _match_firmware(_load_firmware_registry().get("firmware", []), vendor, product)
    prof = (entry or {}).get("profile")
    if not prof:
        return None

    parts: list[str] = []
    degraded: list[str] = []
    if prof.get("video"):
        parts.append(f"video {prof['video']}")
    if "mouse" in prof:
        parts.append(f"mouse={prof['mouse']}")
        if prof["mouse"] != "absolute":
            degraded.append("no absolute mouse — GUI pointer control is degraded")
    if "vmedia" in prof:
        parts.append(f"vmedia={prof['vmedia']}")
        if prof["vmedia"] != "reliable":
            degraded.append(f"virtual media is {prof['vmedia']} — boot-from-ISO may not reach the host")
    if "power_state_trusted" in prof:
        trusted = bool(prof["power_state_trusted"])
        parts.append(f"power readings {'trusted' if trusted else 'NOT trusted'}")
        if not trusted:
            degraded.append("power/LED readings are not trustworthy — verify state visually, don't automate blind reboots")
    if not parts:
        return None
    return CheckResult(
        id="capability-profile",
        pillar=Pillar.READINESS,
        severity=Severity.WARNING if degraded else Severity.INFO,
        title="Capability / expected UX",
        detail=f"Expected experience on {product}: " + ", ".join(parts) + ".",
        remediation="; ".join(degraded),
    )


def check_support_evidence(driver: Any) -> CheckResult | None:
    """Report what has (and has NOT) been exercised live on this exact
    device+firmware, from the run ledger shipped in the package (#102, part of
    #96, evidence source #105).

    Evidence, never hand-set levels: the raw pass/fail counts come from
    ``support_matrix.rollup``, and the maturity label riding along is #98's
    *derived* level joined from the shipped registry — this check computes no
    level itself (that would recreate the drift #98's CI gate prevents). INFO
    by default: "no live evidence" is the near-universal state for an alpha
    and must not inflate reports or feed the destructive gate (same rationale
    as ssh-reachable). WARNING only when a recorded live attempt actually
    FAILED on this firmware (e.g. RM1PE V1.5.1 firmware_update, #94/#95).
    """
    fn = getattr(driver, "get_firmware_info", None)
    if fn is None:
        return None
    try:
        fw = fn()
    except KVMPilotError:
        return None
    vendor = (fw.get("vendor") or "").strip()
    product = fw.get("product") or ""
    version = fw.get("version") or ""
    if not (vendor and product and version):
        return None
    from .support_matrix import CONDITION_SENSITIVE_CAPS, DESTRUCTIVE_CAPS, rollup

    # exact_product: this check speaks for THIS device+firmware — the tool's
    # substring match would otherwise report a sibling's evidence (e.g. an
    # "RM1" ledger row for an "RM1PE" device) under this device's name.
    rows = rollup(
        vendor=vendor, product=product, firmware_version=version, exact_product=True
    )
    if not rows:
        return CheckResult(
            id="support-evidence",
            pillar=Pillar.READINESS,
            severity=Severity.INFO,
            title="Live-test evidence",
            detail=(
                f"No live test runs recorded for {product} {version} — every "
                "capability on this exact device+firmware is unverified "
                "(mock-only maturity)."
            ),
            remediation=(
                "Treat results as unverified; see the support_matrix MCP tool / "
                "the wiki Hardware-Compatibility page for exercised combos."
            ),
        )
    row = rows[0]
    caps = row["capabilities"]
    passing = [c for c, s in caps.items() if not s["fails"]]
    failing = [c for c, s in caps.items() if s["fails"]]

    def _cap_summary(c: str) -> str:
        # Conditions (#156) make the evidence self-explaining: "snapshot (n=2)"
        # verified at 1024x768 says nothing about 2560x1440 (#180).
        s = caps[c]
        conds = ", ".join(s.get("pass_conditions", []))
        return f"{c} (n={s['passes']}{f' @ {conds}' if conds else ''})"

    parts: list[str] = []
    if passing:
        parts.append("recorded pass (run ledger): " + ", ".join(map(_cap_summary, passing)))
    if failing:

        def _fail_summary(c: str) -> str:
            # A mixed capability shows BOTH operating points — pass at one
            # (resolution × encoder) and fail at another is one consistent
            # story, not a contradiction (#156/#180).
            s = caps[c]
            bits = [f"{s['passes']}/{s['passes'] + s['fails']}"]
            if s.get("pass_conditions"):
                bits.append(f"pass @ {', '.join(s['pass_conditions'])}")
            if s.get("fail_conditions"):
                bits.append(f"FAIL @ {', '.join(s['fail_conditions'])}")
            return f"{c} ({'; '.join(bits)} — {s['last_outcome']})"

        parts.append("recorded FAIL: " + ", ".join(map(_fail_summary, failing)))
    if row["never_exercised"]:
        parts.append("never exercised live: " + ", ".join(row["never_exercised"]))
    # Condition-blind evidence gave the #180 false confidence: a bare
    # "verified" observed at a JPEG-friendly resolution hid that the same
    # firmware fails at native res. Say so instead of overclaiming, for every
    # capability the schema declares condition-sensitive.
    for c in sorted(CONDITION_SENSITIVE_CAPS):
        if c in passing and not caps[c].get("pass_conditions"):
            parts.append(
                f"{c} evidence carries no recorded conditions — verified only "
                "under unknown resolution/encoder; may still fail at native res (#180)"
            )
    parts.append(
        "these are RECORDED results, not this run's live probes — the "
        "tested-now findings are this report's own checks (video-signal, "
        "hid-reachable, encoder-wedge)"
    )
    maturity = row.get("maturity")
    label = f" (maturity {maturity['level']}, derived #98)" if maturity else ""
    unproven = [c for c in failing + row["never_exercised"] if c in DESTRUCTIVE_CAPS]
    return CheckResult(
        id="support-evidence",
        pillar=Pillar.READINESS,
        severity=Severity.WARNING if failing else Severity.INFO,
        title="Live-test evidence (recorded)",
        detail=f"Recorded evidence for {product} {version}{label}: " + "; ".join(parts) + ".",
        remediation=(
            f"Do not rely on {', '.join(unproven)} on this firmware without "
            "operator confirmation."
            if unproven
            else ""
        ),
    )


# The registry — each check self-guards, so the same list serves every driver.
CHECKS: list[Check] = [
    check_api_reachable,
    check_driver_identity,
    check_ssh_reachable,
    check_recovery_path,
    check_video_signal,
    check_hid_reachable,
    check_encoder_wedge,
    check_msd_online,
    check_tls_posture,
    check_default_creds,
    check_exposed_services,
    check_amt_transport_security,
    check_amt_provisioning,
    check_amt_redirection,
    check_amt_kvm_consent,
    check_amt_rfb_password,
    check_firmware_report,
    check_firmware_quirks,
    check_firmware_currency,
    check_capability_profile,
    check_support_evidence,
]


def run_healthcheck(driver: Any, *, checks: Iterable[Check] | None = None) -> HealthReport:
    """Run every applicable check against ``driver`` and return a report."""
    results: list[CheckResult] = []
    for check in checks if checks is not None else CHECKS:
        try:
            res = check(driver)
        except Exception as exc:  # a broken check must never crash the audit
            res = CheckResult(
                id=getattr(check, "__name__", "check"),
                pillar=Pillar.READINESS,
                severity=Severity.INFO,
                title="Check error",
                detail=f"{type(exc).__name__}: {exc}",
                cacheable=False,
            )
        if res is not None:
            results.append(res)
    return HealthReport(
        host=getattr(driver, "host", "?"),
        driver_kind=_driver_kind(driver),
        firmware=_firmware_of(driver),
        results=results,
        ran_at=_now(),
    )


def _now() -> float:
    # Wrapped so tests can monkeypatch a deterministic clock.
    return time.time()


# --------------------------------------------------------------------------- #
# Severity gate                                                              #
# --------------------------------------------------------------------------- #


def enforce_gate(
    report: HealthReport,
    *,
    confirm: ConfirmCallback | None = None,
    skip: bool = False,
    acknowledged: frozenset[str] = frozenset(),
) -> None:
    """Block a destructive op on unacknowledged CRITICAL findings.

    ``skip`` bypasses the gate. Findings whose id is in ``acknowledged`` (a
    stored override) are treated as accepted. With remaining criticals:

    * ``confirm`` given (interactive) → prompt continue/abort.
    * ``confirm`` None (automation) → **fail closed** (raise).
    """
    if skip:
        return
    pending = [c for c in report.criticals if c.id not in acknowledged]
    if not pending:
        return
    summary = "; ".join(f"{c.title}: {c.detail}" for c in pending)
    message = (
        f"{len(pending)} CRITICAL health finding(s) on {report.cache_key}: {summary}"
    )
    if confirm is None:
        raise HealthGateError(message)
    if not confirm("health.gate", message):
        raise HealthGateError(f"Operation aborted by operator. {message}")


# --------------------------------------------------------------------------- #
# Cache (stable posture only) + acknowledgements                            #
# --------------------------------------------------------------------------- #


def _cache_base_dir(name: str = os.name) -> str:
    if name == "nt":  # pragma: no cover - platform specific
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return base
        return str(Path.home() / "AppData" / "Local")
    return os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")


def _default_cache_path() -> Path:
    override = os.environ.get("KVM_PILOT_HEALTH_CACHE")
    if override:
        return Path(override).expanduser()
    return Path(_cache_base_dir()) / "kvm-pilot" / "health-cache.json"


class HealthCache:
    """Persist the **stable** posture + operator acknowledgements per device.

    Keyed by ``driver@host#firmware`` so a firmware change invalidates the entry
    automatically — that is what catches the GL "an upgrade silently reverted my
    settings" trap. Volatile results are never stored here.
    """

    def __init__(self, path: Path | None = None, *, max_age: float = 86400.0) -> None:
        self.path = path or _default_cache_path()
        self.max_age = max_age
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2))
        except OSError:  # pragma: no cover - best effort
            pass

    @staticmethod
    def _results_of(entry: dict[str, Any]) -> list[CheckResult]:
        return [
            CheckResult(
                id=r["id"],
                pillar=Pillar(r["pillar"]),
                severity=Severity[r["severity"]],
                title=r["title"],
                detail=r["detail"],
                remediation=r.get("remediation", ""),
                cacheable=True,
            )
            for r in entry.get("results", [])
        ]

    def stable_results(self, key: str, *, now: float | None = None) -> list[CheckResult] | None:
        entry = self._data.get(key)
        if not entry:
            return None
        clock = now if now is not None else _now()
        if clock - entry.get("ran_at", 0.0) > self.max_age:
            return None
        return self._results_of(entry)

    def results_any_age(self, key: str) -> list[CheckResult] | None:
        """The stored results regardless of ``max_age`` — the firmware-delta
        diff (#180) wants the old-firmware posture even when it has gone stale."""
        entry = self._data.get(key)
        return self._results_of(entry) if entry else None

    def last_assessed(self, device_key: str) -> str | None:
        """Firmware version this device was last assessed at (#180), or None."""
        entry = self._data.get(f"assessed:{device_key}") or {}
        fw = entry.get("firmware")
        return str(fw) if fw else None

    def record_assessed(self, device_key: str, firmware: str) -> None:
        """Remember the firmware a completed assessment saw (#180). Stored under
        ``assessed:{driver}@{host}`` — distinct from the ``driver@host#firmware``
        report keys, so it survives (and detects) the firmware changing.
        No-op when unchanged: preflight runs on every connect, and the warm
        path must stay a zero-write path."""
        if self.last_assessed(device_key) == firmware:
            return
        self._data[f"assessed:{device_key}"] = {"firmware": firmware, "at": _now()}
        self._save()

    def store_stable(self, report: HealthReport) -> None:
        self._data.setdefault(report.cache_key, {})
        self._data[report.cache_key].update(
            {
                "ran_at": report.ran_at,
                "results": [
                    r.to_dict() for r in report.results if r.cacheable
                ],
            }
        )
        self._save()

    def acknowledged(self, key: str) -> frozenset[str]:
        entry = self._data.get(key) or {}
        return frozenset(entry.get("acknowledged", []))

    def acknowledge(self, key: str, ids: Iterable[str]) -> None:
        entry = self._data.setdefault(key, {})
        acks = set(entry.get("acknowledged", []))
        acks.update(ids)
        entry["acknowledged"] = sorted(acks)
        self._save()


def _firmware_delta_result(
    prev_fw: str,
    firmware: str,
    old: list[CheckResult] | None,
    new: list[CheckResult],
) -> CheckResult:
    """The "device changed → reassessed" diff a firmware delta must emit (#180).

    Compares findings at WARNING+ by check id: present before and absent/below
    now = cleared; the inverse = new/regressed. Severity is WARNING when
    anything regressed, else INFO — the delta itself is information, not a fault.
    Both sides must be the cacheable (stable-posture) results only: the cache
    never stored the old run's volatile findings, so including the new run's
    would report every live warning as a false "regression".
    """
    regressed: list[str] = []
    if old is None:
        detail = (
            f"firmware changed {prev_fw} → {firmware}; no prior assessment "
            "retained — full re-assessment run"
        )
    else:
        new_bad = {r.id for r in new if r.severity >= Severity.WARNING}
        old_bad = {r.id for r in old if r.severity >= Severity.WARNING}
        regressed = sorted(new_bad - old_bad)
        detail = f"{prev_fw} → {firmware}: " + "; ".join([
            f"cleared: {', '.join(sorted(old_bad - new_bad)) or 'none'}",
            f"new/regressed: {', '.join(regressed) or 'none'}",
            f"still open: {', '.join(sorted(old_bad & new_bad)) or 'none'}",
        ])
    return CheckResult(
        id="firmware-delta",
        pillar=Pillar.FIRMWARE,
        severity=Severity.WARNING if regressed else Severity.INFO,
        title=f"Firmware changed ({prev_fw} → {firmware}) — reassessed",
        detail=detail,
        remediation=(
            "Re-verify the capabilities you rely on live — recorded evidence "
            "predates this firmware" if regressed else ""
        ),
        cacheable=False,
    )


def preflight(
    driver: Any,
    *,
    confirm: ConfirmCallback | None = None,
    skip: bool = False,
    cache: HealthCache | None = None,
    enforce: bool = True,
) -> HealthReport:
    """Run the audit (stable-from-cache + volatile-live) and enforce the gate.

    Volatile checks always run live; stable checks are served from ``cache`` when
    fresh, else run and re-cached. A firmware version differing from the last
    assessed one invalidates the assessment: the stable checks re-run live and
    the report carries a ``firmware-delta`` diff of what cleared/regressed
    (#180 — an out-of-band web-UI flash must not inherit stale confidence).
    Returns the merged report; raises :class:`HealthGateError` when a critical
    is unacknowledged (unless ``skip``).
    """
    if skip:
        report = HealthReport(host=getattr(driver, "host", "?"), driver_kind=_driver_kind(driver), firmware=None)
        return report

    volatile_checks = [c for c in CHECKS if _is_volatile(c)]
    stable_checks = [c for c in CHECKS if not _is_volatile(c)]

    firmware = _firmware_of(driver)
    device_key = f"{_driver_kind(driver)}@{getattr(driver, 'host', '?')}"
    key = f"{device_key}#{firmware or '?'}"

    prev_fw = cache.last_assessed(device_key) if cache is not None else None
    fw_changed = bool(prev_fw and firmware and prev_fw != firmware)

    cached_stable = None
    if cache is not None and not fw_changed:
        cached_stable = cache.stable_results(key)
    if cached_stable is None:
        stable = run_healthcheck(driver, checks=stable_checks).results
    else:
        stable = cached_stable

    volatile = run_healthcheck(driver, checks=volatile_checks).results

    results = stable + volatile
    if fw_changed and cache is not None and prev_fw is not None:
        old = cache.results_any_age(f"{device_key}#{prev_fw}")
        new_stable = [r for r in results if r.cacheable]
        results = results + [_firmware_delta_result(prev_fw, firmware or "?", old, new_stable)]

    report = HealthReport(
        host=getattr(driver, "host", "?"),
        driver_kind=_driver_kind(driver),
        firmware=firmware,
        results=results,
        ran_at=_now(),
    )
    if cache is not None and cached_stable is None:
        cache.store_stable(report)
    if cache is not None and firmware:
        cache.record_assessed(device_key, firmware)

    acknowledged = cache.acknowledged(key) if cache is not None else frozenset()
    if enforce:
        enforce_gate(report, confirm=confirm, acknowledged=acknowledged)
    return report


# --------------------------------------------------------------------------- #
# First-connection audit (issue #80)                                          #
# --------------------------------------------------------------------------- #
#
# #80 wants the audit to run "on the first connection to any KVM, before it's
# used for anything" — not only ahead of a destructive op. A long-lived process
# (the MCP server builds+closes a driver per tool call) must still audit a given
# device only once, so an in-memory guard debounces within the process. The
# persistent HealthCache handles staleness across processes; this is orthogonal.

_SESSION_AUDITED: set[str] = set()


def _session_key(driver: Any) -> str:
    # Host identity alone means "already connected this session" — no firmware,
    # so the guard needs no network probe; firmware-change invalidation across
    # processes is the persistent cache's job (keyed driver@host#firmware).
    return f"{_driver_kind(driver)}@{getattr(driver, 'host', '?')}"


def reset_session_audit() -> None:
    """Forget which devices were audited this process (tests / forced re-audit)."""
    _SESSION_AUDITED.clear()


def note_session_audited(driver: Any) -> None:
    """Record that this device was just audited outside :func:`preflight_once`.

    An explicit healthcheck (#225) satisfies the once-per-session audit; without
    this, the next tool call would immediately re-probe the same device.
    """
    _SESSION_AUDITED.add(_session_key(driver))


def preflight_once(
    driver: Any,
    *,
    confirm: ConfirmCallback | None = None,
    skip: bool = False,
    cache: HealthCache | None = None,
    enforce: bool = True,
) -> HealthReport | None:
    """Run :func:`preflight` the first time this process connects to a device.

    Returns the report on the first call for a given device and ``None`` on later
    calls (already audited this session) or when ``skip``. Gate semantics are
    :func:`preflight`'s; when ``enforce`` raises, the device is left un-recorded
    so the next attempt re-checks rather than silently proceeding.
    """
    if skip:
        return None
    key = _session_key(driver)
    if key in _SESSION_AUDITED:
        return None
    report = preflight(driver, confirm=confirm, cache=cache, enforce=enforce)
    _SESSION_AUDITED.add(key)
    return report


# Volatile checks are re-probed live every time; stable ones may come from cache.
def _is_volatile(check: Check) -> bool:
    return getattr(check, "__name__", "") in {
        "check_api_reachable",
        "check_ssh_reachable",
        "check_video_signal",
        "check_encoder_wedge",
        "check_msd_online",
    }
