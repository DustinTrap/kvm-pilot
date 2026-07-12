"""
Experimental MCP server for kvm-pilot.

Exposes a KVM device to MCP-capable agents (Claude Desktop, etc.) over stdio.
It ships in the wheel (``pip install kvm-pilot``) and is launched via the
``kvm-pilot-mcp`` console script or ``python -m kvm_pilot.mcp.server``. It depends
on the `mcp` SDK, which is a base dependency (the client/driver code itself stays
stdlib-only, importing ``mcp`` only here).

SAFETY MODEL (see the co-located README.md for the operator-facing version):
  * The read-only tools (``info``, ``healthcheck``, ``capabilities``,
    ``support_matrix``, ``power_state``, ``logs``, ``snapshot``,
    ``classify_screen``, ``wait_for_state``, ``list_virtual_media``,
    ``ssh_reachable``, ``ssh_discover``) run with a
    deny-all confirm callback and carry a ``readOnlyHint`` tool annotation
    (``ssh_discover`` additionally requires ``confirm=true`` — an active
    network scan is read-only but not harmless).
  * The destructive tools (``power``, the HID act tools ``type_text`` /
    ``press_key`` / ``send_shortcut`` / ``ctrl_alt_delete`` / ``mouse``, the
    media tools ``mount_iso`` / ``eject``, ``ssh_exec``, and the
    external-write tool ``file_firmware_report``) carry
    ``destructiveHint`` and are DISABLED until the operator opts the tool's
    *effect class* in via an env flag in the server's own environment
    (``KVM_PILOT_MCP_ALLOW_POWER`` / ``_ALLOW_HID`` / ``_ALLOW_MEDIA`` /
    ``_ALLOW_SSH`` / ``_ALLOW_EXTERNAL_WRITE``). An effect class with no
    registered flag is fail-closed. On top of that each act call requires per-invocation
    approval — a human MCP elicitation when the client supports it, else an
    explicit ``confirm=true`` under the operator's standing policy. Tools are
    classified by effect not transport, so a reboot (``ctrl_alt_delete``, a
    Ctrl+Alt+Del chord) needs the power gate, not the HID gate. See ``act.py``
    and the co-located README. Annotations are hints, never a security boundary.
  * ``KVM_PILOT_MCP_DRY_RUN=1`` builds every driver with ``dry_run=True``:
    destructive calls are logged and skipped, and results say so.
  * Drivers are built per call and closed afterwards — Redfish BMCs cap
    concurrent sessions device-side, so a leaked session locks operators out.

Hardware validation is tracked per device+firmware+capability in the support
matrix (the wiki Hardware-Compatibility page is the source of truth); most
combos remain mock-only, so treat results on unlisted hardware as unverified.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING, Literal, cast

import anyio
from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from kvm_pilot import resolve_host
from kvm_pilot.config import HostConfig
from kvm_pilot.drivers import Capability, KVMDriver, make_driver_from_config
from kvm_pilot.errors import CapabilityError, KVMPilotError, VisionError
from kvm_pilot.errors import TimeoutError as KVMTimeoutError
from kvm_pilot.mcp import act
from kvm_pilot.safety import EffectClass, allow_all, deny_all, shortcut_effect
from kvm_pilot.vision import (
    ALL_PHASES,
    SYSTEM_PROMPT,
    ScreenAnalyzer,
    VisionBackend,
    make_backend,
)
from kvm_pilot.vision.base import PHASE_NO_SIGNAL, PHASE_POWER_OFF, PHASE_UNKNOWN

if TYPE_CHECKING:
    # ``_driver(capability=…)`` guarantees the driver supports the capability at
    # runtime; narrow to the owning protocol for the capability-specific calls
    # (mirrors the ``cast`` pattern in cli.py).
    from kvm_pilot.drivers.base import HID, Logs, Power, SystemInfo, Video, VirtualMedia

mcp = FastMCP("kvm-pilot")
_log = logging.getLogger("kvm_pilot.mcp")

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

_POWER_ACTIONS = {
    "on": "power_on",
    "off": "power_off",
    "off-hard": "power_off_hard",
    "reset": "reset_hard",
}


# Single source of truth for the operator env flags (shared with the act layer).
_env_flag = act.env_flag


def _dry_run() -> bool:
    return _env_flag("KVM_PILOT_MCP_DRY_RUN")


@contextmanager
def _driver(
    profile: str | None,
    *,
    confirm: Callable[[str, str], bool],
    capability: Capability | None = None,
    enforce_health: bool = False,
    preflight: bool = True,
) -> Iterator[tuple[HostConfig, KVMDriver]]:
    """Resolve the profile, build its driver, and always close it afterwards.

    The capability gate is checked structurally (``supports()``, no network) so a
    tool the driver cannot serve fails with a clear MCP tool error instead of an
    ``AttributeError`` — e.g. a Redfish BMC has no video capture. ``capability=None``
    skips the gate for meta tools that every driver serves (e.g. ``capabilities``).

    Runs the device preflight healthcheck (#80) on first connection: read-only
    tools inform (once per device, non-blocking); ``enforce_health`` (the ``power``
    tool) fails closed on an unacknowledged CRITICAL. ``preflight=False`` skips it
    for structural, offline tools that never touch the network.
    """
    act.enforce_allowlist(profile)  # fail-closed KVM_PILOT_MCP_PROFILES; raises ToolError
    cfg = resolve_host(profile or os.environ.get("KVM_PILOT_PROFILE"))
    kvm = make_driver_from_config(cfg, confirm=confirm, dry_run=_dry_run())
    try:
        if capability is not None and not kvm.supports(capability):
            raise ToolError(
                f"the '{cfg.driver}' driver for host '{cfg.host}' does not provide the "
                f"'{capability.value}' capability this tool needs"
            )
        if preflight:
            _preflight(kvm, enforce=enforce_health)
        yield cfg, kvm
    finally:
        close = getattr(kvm, "close", None)
        if close is not None:
            close()


def _preflight(kvm: KVMDriver, *, enforce: bool) -> None:
    """Audit the device on first connection (#80).

    Skipped under dry-run or ``KVM_PILOT_SKIP_HEALTHCHECK``. When ``enforce``, a
    CRITICAL fails closed (automation has no operator to prompt) and surfaces as a
    tool error; otherwise findings are audited once per device and logged, never
    blocking a read.
    """
    if _dry_run() or _env_flag("KVM_PILOT_SKIP_HEALTHCHECK"):
        return
    from kvm_pilot.health import (
        HealthCache,
        HealthGateError,
        Severity,
        preflight,
        preflight_once,
    )

    cache = HealthCache()
    if enforce:
        try:
            # confirm=None -> automation fails closed on an unacknowledged critical.
            preflight(kvm, confirm=None, cache=cache, enforce=True)
        except HealthGateError as exc:
            raise ToolError(f"device preflight blocked this operation: {exc}") from exc
        return
    try:
        report = preflight_once(kvm, cache=cache, enforce=False)
    except Exception:  # noqa: BLE001 - an informational audit must never break a read
        return
    if report is None:
        return
    notable = [r for r in report.results if r.severity >= Severity.WARNING]
    if notable:
        _log.warning(
            "preflight %s@%s: worst %s, %d finding(s): %s",
            report.driver_kind,
            report.host,
            report.worst,
            len(notable),
            "; ".join(f"{r.title}: {r.detail}" for r in notable),
        )


def _provenance(cfg: HostConfig) -> dict:
    """Every result names the host/driver it acted on (and flags dry-run)."""
    out: dict = {"host": cfg.host, "driver": cfg.driver}
    if _dry_run():
        out["dry_run"] = True
    return out


# -- vision backend (cached once per process) --------------------------------
#
# Same selection env vars as the CLI flags: KVM_PILOT_VISION_BACKEND mirrors
# --backend, KVM_PILOT_VISION_URL mirrors --vision-url, and the pre-existing
# KVM_PILOT_VISION_MODEL mirrors --vision-model. Caching the backend means the
# Anthropic model auto-resolution happens once per process, not per tool call.
# Drivers stay per-call (see _driver).

_vision_lock = threading.Lock()
_vision: VisionBackend | None = None


def _vision_backend() -> VisionBackend:
    global _vision
    with _vision_lock:
        if _vision is None:
            kind = os.environ.get("KVM_PILOT_VISION_BACKEND", "anthropic").lower()
            if kind in ("anthropic", "claude"):
                # Resolves ANTHROPIC_API_KEY / KVM_PILOT_VISION_MODEL itself.
                _vision = make_backend(kind)
            else:
                _vision = make_backend(
                    kind,
                    base_url=os.environ.get("KVM_PILOT_VISION_URL"),
                    model=os.environ.get("KVM_PILOT_VISION_MODEL"),
                )
        return _vision


# -- tools --------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY)
def info(profile: str | None = None) -> dict:
    """Return device / system info (read-only)."""
    with _driver(profile, confirm=deny_all, capability=Capability.SYSTEM_INFO) as (cfg, kvm):
        return {**_provenance(cfg), "info": cast("SystemInfo", kvm).get_info()}


@mcp.tool(annotations=_READ_ONLY)
def healthcheck(profile: str | None = None) -> dict:
    """Audit the device's readiness/recovery, security posture, and firmware (#80).

    Read-only. Returns per-check findings with a tiered severity; a ``CRITICAL``
    (e.g. no out-of-band reset path) is what should gate a subsequent destructive
    op. The most valuable finding is ``recovery-path`` — whether a hung guest can
    be reset at all when the KVM is remote.
    """
    with _driver(profile, confirm=deny_all, capability=Capability.SYSTEM_INFO) as (cfg, kvm):
        from kvm_pilot.health import run_healthcheck

        return {**_provenance(cfg), **run_healthcheck(kvm).to_dict()}


@mcp.tool(annotations=_READ_ONLY)
def capabilities(profile: str | None = None) -> dict:
    """List the capabilities the target's driver supports (read-only, offline).

    Structural — makes no network call and runs no preflight; it answers "which
    tools/actions can this device serve?" so you can pick the right interface up
    front (a Redfish BMC has no video; a PiKVM has no BootProgress). Returned in
    the capability enum's declaration order for stable output. ``live_evidence``
    additionally names which device+firmware combos this driver has real-hardware
    run evidence for — structural support is not live verification; call
    ``support_matrix`` for per-combo evidence and ``healthcheck`` for this exact
    device+firmware.
    """
    with _driver(profile, confirm=deny_all, preflight=False) as (cfg, kvm):
        from kvm_pilot.support_matrix import rollup

        caps = kvm.capabilities()
        exercised = rollup(driver=cfg.driver)
        return {
            **_provenance(cfg),
            "capabilities": [c.value for c in Capability if c in caps],
            "live_evidence": {
                "combos": [
                    f"{r['vendor']} {r['product']} {r['firmware_version']}" for r in exercised
                ],
                "note": (
                    "structural capability != live-verified; call support_matrix for "
                    "per-combo evidence and healthcheck for this device+firmware"
                ),
            },
        }


@mcp.tool(annotations=_READ_ONLY)
def support_matrix(
    vendor: str | None = None,
    product: str | None = None,
    firmware_version: str | None = None,
) -> dict:
    """What has actually been exercised on real hardware, per
    device+firmware+capability (read-only, offline — no device call).

    Aggregated from the test-run ledger shipped in the package (the same data
    behind the wiki Hardware-Compatibility page), with each combo's derived
    maturity level (#98) joined from the shipped firmware registry. This is
    EVIDENCE, not a guarantee: a capability listed in ``never_exercised`` (or a
    combo with no row at all) is unverified on that hardware — treat it as
    mock-only/alpha maturity and confirm destructive steps with the user.
    ``status`` is "fail" when every recorded live attempt failed (e.g. RM1PE
    V1.5.1 firmware_update, #94/#95). Filters are case-insensitive; ``product``
    matches as a substring.
    """
    from kvm_pilot.support_matrix import rollup

    return {
        "combos": rollup(vendor=vendor, product=product, firmware_version=firmware_version),
        "note": (
            "capabilities absent from a combo (or combos absent entirely) are "
            "UNVERIFIED live; 'maturity' is the #98-derived level from the shipped "
            "registry (null when the ledger backs no derived row)"
        ),
    }


@mcp.tool(annotations=_READ_ONLY)
def power_state(profile: str | None = None) -> dict:
    """Return whether the host is powered on, plus ATX detail where the driver has it
    (read-only)."""
    with _driver(profile, confirm=deny_all, capability=Capability.POWER) as (cfg, kvm):
        state = {**_provenance(cfg), "powered_on": cast("Power", kvm).is_powered_on()}
        # ATX detail is PiKVM-family only; Redfish/fake answer via is_powered_on().
        if hasattr(kvm, "get_atx_state"):
            state["atx"] = kvm.get_atx_state()
        return state


@mcp.tool(annotations=_READ_ONLY)
def logs(seek: int = 0, profile: str | None = None) -> dict:
    """Return the device/host event log as text (read-only).

    ``seek`` is seconds of lookback (0 = the whole buffer). This is the go-to
    diagnostic when video/streamer/encoder or power behaviour looks wrong: the
    text log names a fault (e.g. a stuck encoder behind a ``snapshot`` 503) that
    a screenshot cannot. Tail-follow is intentionally not exposed — it blocks
    over the server's synchronous transport.
    """
    with _driver(profile, confirm=deny_all, capability=Capability.LOGS) as (cfg, kvm):
        return {**_provenance(cfg), "log": cast("Logs", kvm).get_logs(seek=seek)}


@mcp.tool(annotations=_READ_ONLY)
def snapshot(profile: str | None = None):
    """Capture the current KVM screen (read-only).

    Returns ``[json_text, image]``: the JSON carries a ``frame_ref``
    (``host:generation:hash``) — pass it back to the ``mouse`` tool as
    ``observed_frame_ref`` so an absolute click can be refused if the host rebooted
    or swapped media since you looked.

    It also carries the live ``signal`` state (online/resolution/fps/format,
    #143) and ``unchanged_since_last_snapshot`` (#141): a byte-identical frame
    when the screen should have changed means the pixels are stale/cached — do
    NOT trust them as ground truth; check ``signal`` and ``logs`` instead.
    """
    with _driver(profile, confirm=deny_all, capability=Capability.VIDEO) as (cfg, kvm):
        img = cast("Video", kvm).snapshot()
        note = f"screen of host '{cfg.host}' via the '{cfg.driver}' driver"
        if _dry_run():
            note += " (dry-run session)"
        ref = act.frame_ref(cfg.host, img)
        payload = {
            **_provenance(cfg),
            "frame_ref": ref,
            "unchanged_since_last_snapshot": act.note_frame(cfg.host, ref),
            "note": note,
        }
        if payload["unchanged_since_last_snapshot"]:
            payload["staleness_note"] = (
                "byte-identical to the previous snapshot — if the screen should "
                "have changed since, treat this frame as possibly stale/cached "
                "(#141) and verify via `signal` + `logs` before acting on it"
            )
        if hasattr(kvm, "video_signal_info"):
            try:
                payload["signal"] = kvm.video_signal_info()
            except KVMPilotError:
                payload["signal"] = None  # snapshot worked; a state probe must not fail it
        return [json.dumps(payload), Image(data=img, format="jpeg")]


@mcp.tool(annotations=_READ_ONLY)
def classify_screen(hint: str = "", profile: str | None = None):
    """Classify the current screen's boot/run phase (read-only).

    Uses the server-side vision backend when one is configured. If the server has
    no vision credentials (no ``ANTHROPIC_API_KEY`` / ``KVM_PILOT_VISION_*``), it
    falls back to *caller-side* classification: it returns the screenshot plus the
    classification prompt/schema so a vision-capable agent can classify the image
    itself. Cheap on-device gates (power-off, no-signal, boot-progress, OCR rules)
    still resolve with no credentials at all.

    Return shapes:
      * server-side / cheap-gate → a dict with ``mode="server"`` + phase fields.
      * caller-side fallback → a ``[json_text, Image]`` list; classify the image
        yourself against the ``system_prompt`` / ``phases`` in the JSON block.
    """
    with _driver(profile, confirm=deny_all, capability=Capability.VIDEO) as (cfg, kvm):
        try:
            state = ScreenAnalyzer(kvm, _vision_backend()).classify(hint=hint)
        except VisionError as exc:
            return _classify_fallback(cfg, kvm, hint, exc)
        return {**_provenance(cfg), "mode": "server", **state.to_dict()}


def _classify_fallback(cfg: HostConfig, kvm: KVMDriver, hint: str, exc: VisionError):
    """Return the screenshot + classification prompt for caller-side vision (#125).

    Server-side vision is unavailable (no key/model, or a backend failure); the
    calling agent already has vision, so hand it the frame and the same
    prompt/schema ``classify_screen`` would have used. If we can't even capture a
    frame there is nothing to delegate — surface a tool error instead.
    """
    try:
        img = cast("Video", kvm).snapshot()
    except Exception as e:  # noqa: BLE001 - no image means nothing to delegate
        raise ToolError(
            f"server-side vision unavailable ({exc}); a fallback snapshot also failed: {e}"
        ) from e
    payload = {
        **_provenance(cfg),
        "mode": "caller_classify",
        "reason": str(exc),
        "hint": hint,
        "instructions": (
            "Server-side vision is unavailable. Classify the attached screenshot and "
            "reply with a JSON object matching the schema in 'system_prompt'."
        ),
        "phases": ALL_PHASES,
        "system_prompt": SYSTEM_PROMPT,
    }
    return [json.dumps(payload), Image(data=img, format="jpeg")]


# -- wait_for_state (#147): screen-phase wait parity with CLI watch -----------

_WAIT_TIMEOUT_CAP = 300.0  # hard server-side ceiling; chain calls for longer waits


def _clamp_timeout(timeout: float) -> float:
    # NaN slips through a bare `<= 0` (all NaN comparisons are False) and would
    # propagate into min() and the deadline math — reject anything non-finite.
    if not math.isfinite(timeout) or timeout <= 0:
        raise ToolError("timeout must be a finite number > 0 seconds")
    return min(timeout, _WAIT_TIMEOUT_CAP)


def _no_vision_error(phase: str, reason: str) -> ToolError:
    return ToolError(
        f"cannot wait for {phase!r}: resolving this phase needs the vision model, but "
        f"server-side vision is unavailable ({reason}). Poll `classify_screen` instead — "
        "with no server key it returns the screenshot plus the classification prompt so a "
        "vision-capable agent can classify each frame itself (caller-side)."
    )


def _keyless_waitable_phases(kvm: KVMDriver) -> set[str]:
    """The phases the cheap gates can EVER emit for this driver.

    A keyless server can legitimately wait for exactly these (#147): the
    power/signal probes emit ``power_off`` / ``no_signal``, and a
    BootProgress-capable driver (BMCs, the fake) can structurally report any
    phase token. Everything else needs the vision model. The gate is on the
    TARGET phase, never the current frame: waiting keylessly for ``power_off``
    must work even while the screen currently shows a desktop the cheap gates
    can't classify — those interim polls just retry until the target's own
    gate fires.
    """
    phases: set[str] = set()
    if hasattr(kvm, "is_powered_on"):
        phases.add(PHASE_POWER_OFF)
    if hasattr(kvm, "has_video_signal"):
        phases.add(PHASE_NO_SIGNAL)
    if getattr(kvm, "get_boot_progress", None) is not None:
        # BootProgress maps a device's structured state to a phase token, but
        # never to PHASE_UNKNOWN (_probe_boot_progress drops that), so it is not
        # keylessly waitable — exclude it from the structural set.
        phases.update(p for p in ALL_PHASES if p != PHASE_UNKNOWN)
    return phases


def _final_frame_ref(cfg: HostConfig, kvm: KVMDriver, state) -> str | None:
    """Mint a frame_ref for the final frame so a follow-up mouse click can anchor.

    Cheap-gate states carry no image (power_off/no_signal resolve without a
    snapshot); take one best-effort frame then. None if even that fails (e.g. the
    streamer 503s on a powered-off host) — the wait result must not fail because
    the anchor could not be minted.
    """
    if state.image_b64:
        return act.frame_ref(cfg.host, base64.b64decode(state.image_b64))
    try:
        return act.frame_ref(cfg.host, cast("Video", kvm).snapshot())
    except Exception:  # noqa: BLE001
        return None


@mcp.tool(annotations=_READ_ONLY)
async def wait_for_state(
    ctx: Context,
    phase: str,
    timeout: float = 60.0,
    hint: str = "",
    profile: str | None = None,
) -> dict:
    """Wait (bounded) until the screen reaches a boot/run phase (read-only).

    The server-side twin of the CLI `watch` command: polls the sensing hierarchy
    (cheap power/signal/boot-progress gates first, then the server-side vision
    backend) with backoff until `phase` is observed at sufficient confidence, so
    an agent doesn't have to hand-roll `classify_screen` polling round-trips.

    `phase` must be a known phase token (see `classify_screen`); an unknown token
    fails immediately with the valid list. `timeout` is seconds (default 60,
    hard server-side cap 300 — for longer waits call again: the timeout result
    reports the last observed phase, so chaining is cheap). Emits an MCP progress
    notification per poll when the client requests progress.

    Returns `reached=true` with the final phase/confidence plus a `frame_ref` for
    the final frame — pass it to `mouse` as `observed_frame_ref` to anchor a
    follow-up click. A timeout returns `reached=false` with the last observed
    state through the same path (never a hang, never a raised error). If the
    server has no vision credentials, only phases this driver's cheap gates can
    ever emit (`power_off`, `no_signal`, and any phase on a BootProgress-capable
    driver) are waitable — anything else fails fast, directing you to
    `classify_screen` polling (whose caller-side fallback hands you the
    screenshot + prompt). Holds the device driver open for up to `timeout`
    seconds.
    """
    if phase not in ALL_PHASES:
        raise ToolError(f"unknown phase {phase!r}. Valid phases: {', '.join(ALL_PHASES)}")
    timeout = _clamp_timeout(timeout)
    try:
        backend = _vision_backend()
    except VisionError as exc:  # e.g. 'local' kind with no URL/model configured
        raise _no_vision_error(phase, str(exc)) from exc

    def _wait_sync() -> dict:
        # The whole device interaction — driver build, preflight, the poll loop,
        # the final frame ref, close — is blocking I/O, so all of it lives in
        # this worker thread; the event loop stays free for pings/progress.
        with _driver(profile, confirm=deny_all, capability=Capability.VIDEO) as (cfg, kvm):
            if getattr(backend, "credentialed", True) is False:
                waitable = _keyless_waitable_phases(kvm)
                if phase not in waitable:
                    raise _no_vision_error(
                        phase,
                        "no vision credentials configured; this driver's cheap gates "
                        f"can only observe: {', '.join(sorted(waitable)) or 'nothing'}",
                    )
            analyzer = ScreenAnalyzer(kvm, backend)
            last: list = []  # last observed ScreenState, for the timeout result

            def _note(state, elapsed) -> None:
                last[:] = [state]
                try:  # best-effort; no-op when the client sent no progress token
                    anyio.from_thread.run(
                        ctx.report_progress, min(elapsed, timeout), timeout,
                        f"{state.phase} (confidence={state.confidence:.2f})",
                    )
                except Exception:  # noqa: BLE001 - progress must never break the wait
                    pass

            start = time.monotonic()
            # Hold the display awake AND (on GL) keep the on-demand encoder warm for
            # the wait, so the poll loop can't DPMS-sleep (#161) or 503 on a cold
            # streamer (#142); drivers lacking either no-op via nullctx.
            awake = getattr(kvm, "display_awake", nullcontext)
            warm = getattr(kvm, "streamer_warm", nullcontext)
            try:
                with warm(), awake():
                    state = analyzer.wait_for_state(
                        phase, timeout=timeout, hint=hint, on_poll=_note)
            except KVMTimeoutError as exc:
                out = {
                    **_provenance(cfg), "reached": False, "waited_for": phase,
                    "timeout_s": timeout, "detail": str(exc),
                }
                if last:
                    out["last"] = last[0].to_dict()
                return out
            return {
                **_provenance(cfg), "reached": True, "waited_for": phase,
                "elapsed_s": round(time.monotonic() - start, 1),
                **state.to_dict(),
                "frame_ref": _final_frame_ref(cfg, kvm, state),
            }

    return await anyio.to_thread.run_sync(_wait_sync)


# The stable power state each action should land in; reset has no stable target.
_POWER_EXPECTED: dict[str, bool | None] = {
    "on": True, "off": False, "off-hard": False, "reset": None,
}


def _observe_power(kvm: KVMDriver) -> tuple[bool | None, str]:
    """One honest power observation: (state | None, its source / why unverifiable).

    Structural dispatch, no driver-name map: a PiKVM-family driver exposes
    ``get_atx_state`` — when ATX sensing is unwired/absent (``enabled`` falsy,
    which is also what GL units always report: quirk ``atx-power-state-always-off``)
    there is NO trustworthy signal and we say so rather than guessing (the base
    ``is_powered_on`` fail-opens True there, #168). Everything else (Redfish,
    fake) answers via ``is_powered_on`` — authoritative PowerState on a BMC.
    """
    atx_fn = getattr(kvm, "get_atx_state", None)
    if atx_fn is not None:
        try:
            atx = atx_fn() or {}
        except KVMPilotError as exc:
            return None, f"unverifiable: ATX state unreadable ({exc})"
        if not atx.get("enabled"):
            note = (
                "unverifiable: ATX reports enabled=false — no wired power sensing; "
                "verify visually via snapshot or wait_for_state"
            )
            # Name the driver's own quirk when it declares one — the knowledge
            # stays in drivers/*, this generic code just relays it.
            quirks: list = getattr(kvm, "known_quirks", lambda: [])()
            atx_quirk = next((q.id for q in quirks if "atx" in q.id), None)
            if atx_quirk:
                note += f" (device quirk: {atx_quirk})"
            return None, note
        leds = atx.get("leds") or {}
        return bool(leds.get("power")), "ATX power LED"
    try:
        return bool(cast("Power", kvm).is_powered_on()), "power-state readback"
    except KVMPilotError as exc:
        return None, f"unverifiable: power state unreadable ({exc})"


def _verify_power(
    kvm: KVMDriver, action: str, *, timeout: float = 10.0, poll: float = 0.5
) -> tuple[bool | None, bool | None, str]:
    """Bounded poll toward the action's expected state: (verified, observed, note).

    ``verified`` is None (never a guess) when the driver has no trustworthy
    signal or the action has no stable target (``reset``).
    """
    expected = _POWER_EXPECTED[action]
    deadline = time.monotonic() + timeout
    while True:
        observed, source = _observe_power(kvm)
        if observed is None:
            return None, None, source
        if expected is None:
            return None, observed, (
                f"reset issued; no stable target state to verify — "
                f"observed powered_on={observed} via {source}"
            )
        if observed == expected:
            return True, observed, f"reached powered_on={expected} via {source}"
        if time.monotonic() >= deadline:
            return False, observed, (
                f"did NOT reach powered_on={expected} within {timeout:.0f}s — "
                f"still powered_on={observed} via {source}"
            )
        time.sleep(poll)


@mcp.tool(annotations=_DESTRUCTIVE)
def power(
    action: Literal["on", "off", "off-hard", "reset"],
    confirm: bool = False,
    profile: str | None = None,
) -> dict:
    """Change host power state. DESTRUCTIVE.

    Disabled unless the server operator has enabled power control in the server's
    own environment (see the co-located README.md). ``confirm=true`` is required as
    a second factor. The result carries an honest effect report (#168):
    ``verified`` is true/false when the driver has a trustworthy power signal
    (Redfish PowerState, a wired ATX LED), and ``null`` — with the reason and
    what to do instead — when it doesn't (GL units: ATX sensing lies). A power
    action also invalidates prior ``snapshot`` frame refs (generation bump), so
    a stale mouse click can't land on the post-reboot screen.
    """
    if not _env_flag("KVM_PILOT_MCP_ALLOW_POWER"):
        # Deliberately no copy-pasteable variable assignment here: the gate must be
        # opened by the human operator in the server's environment, out of band —
        # an agent must not be able to relay the exact incantation.
        raise ToolError(
            "power control is disabled on this server. Only the human operator can "
            "enable it, by setting the power-enable environment variable (documented "
            "in the server's README.md) in the MCP server's own environment before "
            "starting it. It cannot be enabled from within an agent session."
        )
    if not confirm:
        raise ToolError(f"power {action!r} was not confirmed")
    with _driver(
        profile, confirm=allow_all, capability=Capability.POWER, enforce_health=True
    ) as (cfg, kvm):
        getattr(kvm, _POWER_ACTIONS[action])()
        if _dry_run():
            return {
                **_provenance(cfg), "requested": action, "dry_run": True,
                "verified": None, "observed": None,
                "note": (
                    f"power {action}: DRY-RUN — logged only, nothing was sent to "
                    f"host '{cfg.host}' ({cfg.driver})"
                ),
            }
        # The screen must be presumed changed whether or not verification lands.
        act.bump_generation(cfg.host)
        verified, observed, note = _verify_power(kvm, action)
        return {
            **_provenance(cfg), "requested": action,
            "verified": verified, "observed": observed, "note": note,
            "detail": f"power {action}: requested on host '{cfg.host}' ({cfg.driver})",
        }


# -- HID act tools (issue #61): see act.py for the two-guarantee model --------


def _consume_receipt(
    inv: act.InvocationContext, approval: act.Approval
) -> tuple[act.Receipt, dict | None]:
    """#72 at every dispatch site: mint the single-use receipt and re-verify it
    against the invocation immediately before acting. Returns (receipt, None)
    when dispatch may proceed, else (receipt, denial-result-fields)."""
    receipt = act.issue_receipt(inv, approval)
    denial = act.verify_and_consume(receipt, inv)
    if denial is None:
        return receipt, None
    return receipt, act.result(
        inv,
        act.Approval(False, reason=denial, outcome=act.Outcome.INVALIDATED),
        receipt=receipt,
    )


async def _act(
    ctx: Context,
    profile: str | None,
    *,
    tool: str,
    effect: EffectClass,
    op: str,
    transport: str,
    args: dict,
    confirm: bool,
    run: Callable[[KVMDriver], object],  # return (if any) is ignored
    detail: str,
    capability: Capability = Capability.HID,
) -> dict:
    """Shared act-tool flow (#61): resolve+gate the driver, run the two-guarantee
    approval, execute on approval, and return the receipt. Denials come back
    through the same path (a result with ``approved=False`` + a reason), never a
    raised error, so the agent can recover. Dry-run is handled by the driver's own
    SafetyPolicy, so ``run`` becomes a no-op and the receipt still records intent.
    """
    with _driver(profile, confirm=allow_all, capability=capability) as (cfg, kvm):
        inv = act.new_invocation(
            host=cfg.host, profile=profile, tool=tool, effect=effect, op=op,
            transport=transport, args=args, dry_run=_dry_run(),
        )
        approval = await act.approve_or_deny(ctx, inv, confirm=confirm)
        if not approval.approved:
            return {**_provenance(cfg), **act.result(inv, approval)}
        receipt, denied = _consume_receipt(inv, approval)
        if denied is not None:
            return {**_provenance(cfg), **denied}
        try:
            run(kvm)
        except Exception as exc:
            act.audit_dispatch_error(inv, receipt, exc)
            raise
        # A media/power effect changes the screen enough to invalidate prior
        # observations — bump the frame generation so a stale mouse ref won't match.
        if not inv.dry_run and act.bumps_generation(inv.effect):
            act.bump_generation(cfg.host)
        return {**_provenance(cfg), **act.result(inv, approval, detail=detail, receipt=receipt)}


@mcp.tool(annotations=_DESTRUCTIVE)
async def type_text(
    ctx: Context, text: str, confirm: bool = False, profile: str | None = None
) -> dict:
    """Type ``text`` on the managed host's console over the HID keyboard. DESTRUCTIVE.

    Requires the operator to enable HID (``KVM_PILOT_MCP_ALLOW_HID``) *and* a
    per-invocation approval — a human elicitation when the client supports it, else
    an explicit ``confirm=true`` under the operator's standing policy.
    """
    return await _act(
        ctx, profile, tool="type_text", effect=EffectClass.HID_INPUT, op="hid.type_text",
        transport="hid.keyboard", args={"text": text}, confirm=confirm,
        run=lambda kvm: cast("HID", kvm).type_text(text),
        detail=f"typed {len(text)} character(s)",
    )


@mcp.tool(annotations=_DESTRUCTIVE)
async def press_key(
    ctx: Context, key: str, confirm: bool = False, profile: str | None = None
) -> dict:
    """Press a single key (a kvmd key code, e.g. ``Enter``/``Escape``/``F2``). DESTRUCTIVE.

    Same gating as ``type_text`` (HID input): ``KVM_PILOT_MCP_ALLOW_HID`` + approval.
    """
    return await _act(
        ctx, profile, tool="press_key", effect=EffectClass.HID_INPUT, op="hid.press_key",
        transport="hid.keyboard", args={"key": key}, confirm=confirm,
        run=lambda kvm: cast("HID", kvm).press_key(key),
        detail=f"pressed {key!r}",
    )


@mcp.tool(annotations=_DESTRUCTIVE)
async def send_shortcut(
    ctx: Context, keys: str, confirm: bool = False, profile: str | None = None
) -> dict:
    """Send a key chord — comma-separated kvmd key codes, e.g.
    ``ControlLeft,AltLeft,Delete`` or ``ControlLeft,AltLeft,F2``. DESTRUCTIVE.

    Gated by **effect**, not transport: a reboot/power chord (Ctrl+Alt+Del, Magic
    SysRq) is classified ``power_soft``/``power_hard`` and needs
    ``KVM_PILOT_MCP_ALLOW_POWER``; an ordinary session chord is ``hid_control`` and
    needs ``KVM_PILOT_MCP_ALLOW_HID`` — so a reboot can't slip through the HID gate.
    """
    effect = shortcut_effect(keys)
    return await _act(
        ctx, profile, tool="send_shortcut", effect=effect, op="hid.send_shortcut",
        transport="hid.shortcut", args={"keys": keys}, confirm=confirm,
        run=lambda kvm: cast("HID", kvm).send_shortcut(keys),
        detail=f"sent shortcut {keys!r}",
    )


@mcp.tool(annotations=_DESTRUCTIVE)
async def ctrl_alt_delete(
    ctx: Context, confirm: bool = False, profile: str | None = None
) -> dict:
    """Send Ctrl+Alt+Del to the managed host. DESTRUCTIVE.

    A reboot delivered over the keyboard — classified ``power_soft``, so it needs
    ``KVM_PILOT_MCP_ALLOW_POWER`` (the same gate as the ``power`` tool), never the
    weaker HID gate.
    """
    return await _act(
        ctx, profile, tool="ctrl_alt_delete", effect=EffectClass.POWER_SOFT,
        op="hid.send_shortcut", transport="hid.keyboard", args={}, confirm=confirm,
        run=lambda kvm: cast("HID", kvm).send_shortcut("ControlLeft,AltLeft,Delete"),
        detail="sent Ctrl+Alt+Del",
    )


def _mouse_frame_max_age() -> float:
    """Max age (s) of the observation a mouse click may anchor to (#141).

    Env-overridable (``KVM_PILOT_MCP_FRAME_MAX_AGE``); a generous default so a
    look-then-click stays fluid, tight enough that a minutes-old frame is refused.
    """
    raw = os.environ.get("KVM_PILOT_MCP_FRAME_MAX_AGE", "").strip()
    try:
        val = float(raw)
        return val if val > 0 else 60.0
    except ValueError:
        return 60.0


_MOUSE_FRAME_MAX_AGE = _mouse_frame_max_age()


def _mouse_stale(host: str, observed_frame_ref: str | None) -> str | None:
    """A denial reason if the observation is missing, malformed, from a stale
    generation, or stale by age (#141), else None."""
    if not observed_frame_ref:
        return "a mouse click requires observed_frame_ref from a prior snapshot"
    obs_gen = act.frame_ref_generation(observed_frame_ref)
    if obs_gen is None:
        return f"observed_frame_ref {observed_frame_ref!r} is not a valid frame reference"
    if obs_gen != act.generation(host):
        return (
            "frame was observed before a power/media state change on this host; "
            "re-snapshot and retry so the click can't land on a stale screen"
        )
    # Content-age guard (#141): generation only bumps on power/media, so a frame
    # can go stale while the screen changes on its own. Refuse an observation
    # older than the bound, or one this server never issued (fabricated, or from
    # before a restart) — the agent must click against a fresh, real snapshot.
    age = act.frame_age(observed_frame_ref)
    if age is None:
        return (
            "observed_frame_ref was not issued by this server (fabricated, or the "
            "server restarted since) — take a fresh snapshot and use its frame_ref"
        )
    if age > _MOUSE_FRAME_MAX_AGE:
        return (
            f"observed_frame_ref is {age:.0f}s old (limit {_MOUSE_FRAME_MAX_AGE:.0f}s) — "
            "the screen may have changed since you looked; re-snapshot and retry so "
            "the click can't land on a stale screen"
        )
    return None


def _run_mouse(kvm: KVMDriver, x: float, y: float, coord_space: str, button: str | None) -> None:
    hid = cast("HID", kvm)
    if coord_space == "percent":
        hid.mouse_move(act.pct_to_kvmd(x), act.pct_to_kvmd(y))
    elif coord_space == "raw":
        hid.mouse_move(int(x), int(y))
    else:  # pixel — needs a driver that reports its capture resolution
        move_px = getattr(kvm, "mouse_move_pixels", None)
        if move_px is None:
            raise ToolError(
                "this driver has no pixel-coordinate mouse support; use coord_space='percent'"
            )
        move_px(int(x), int(y))
    if button is not None:
        hid.mouse_click(button)


@mcp.tool(annotations=_DESTRUCTIVE)
async def mouse(
    ctx: Context,
    x: float,
    y: float,
    observed_frame_ref: str | None = None,
    coord_space: Literal["percent", "pixel", "raw"] = "percent",
    button: Literal["left", "right", "middle"] | None = None,
    confirm: bool = False,
    profile: str | None = None,
) -> dict:
    """Move the mouse (and optionally click) on the host. DESTRUCTIVE (HID input).

    Absolute positioning depends on current screen state, so a **click** must carry
    the ``observed_frame_ref`` it was planned against (from a prior ``snapshot``). It
    is refused if the host rebooted or swapped media since (frame *generation*
    changed), if the observation is older than ``KVM_PILOT_MCP_FRAME_MAX_AGE`` (60s
    default — the screen may have changed on its own, #141), or if the ref wasn't
    issued by this server — so the click can't land on a stale screen. Re-``snapshot``
    and retry. A move-only call (``button`` omitted) needs no ref.

    ``coord_space``: ``percent`` (0.0-1.0, default — robust to a resolution change),
    ``pixel`` (screen pixels; needs a driver that reports resolution), or ``raw``
    (kvmd centered -32768..32767). ``button``: omit to move only, else move + click.
    Gated by ``KVM_PILOT_MCP_ALLOW_HID`` + per-invocation approval.
    """
    with _driver(profile, confirm=allow_all, capability=Capability.HID) as (cfg, kvm):
        inv = act.new_invocation(
            host=cfg.host, profile=profile, tool="mouse", effect=EffectClass.HID_INPUT,
            op="hid.mouse_click" if button else "hid.mouse_move", transport="hid.mouse",
            args={"x": x, "y": y, "coord_space": coord_space, "button": button},
            dry_run=_dry_run(),
        )
        # Staleness gate (clicks only): the observation must be from this generation.
        if button is not None:
            stale = _mouse_stale(cfg.host, observed_frame_ref)
            if stale is not None:
                return {**_provenance(cfg), **act.result(inv, act.Approval(False, reason=stale))}
        approval = await act.approve_or_deny(ctx, inv, confirm=confirm)
        if not approval.approved:
            return {**_provenance(cfg), **act.result(inv, approval)}
        receipt, denied = _consume_receipt(inv, approval)
        if denied is not None:
            return {**_provenance(cfg), **denied}
        if not inv.dry_run:
            try:
                _run_mouse(kvm, x, y, coord_space, button)
            except Exception as exc:
                act.audit_dispatch_error(inv, receipt, exc)
                raise
        detail = f"moved to ({x}, {y}) [{coord_space}]" + (f" + {button} click" if button else "")
        return {
            **_provenance(cfg),
            **act.result(inv, approval, detail=detail, extra={"coord_space": coord_space},
                         receipt=receipt),
        }


@mcp.tool(annotations=_READ_ONLY)
def list_virtual_media(profile: str | None = None) -> dict:
    """Inventory the KVM's virtual-media (MSD) storage (read-only).

    Check this BEFORE asking the operator to download or upload an ISO — the
    image you need may already be on the device from an earlier job (#127).
    Returns the device's MSD state: stored images (name/size/completeness),
    the selected drive image, and whether media is attached (``online``).

    ``host_visible_as`` (when the driver knows its brand's gadget name, #78)
    is the device name the TARGET host shows once media is truly presented —
    e.g. a boot-menu entry "UEFI: Glinet Optical Drive 1.00" on GLKVM. Use it
    to pick the right boot entry and as a positive readiness cross-check: a
    generic empty "CD/DVD Drive" entry without this name means the medium is
    not actually inserted.
    """
    with _driver(profile, confirm=deny_all, capability=Capability.VIRTUAL_MEDIA) as (cfg, kvm):
        if not hasattr(kvm, "get_msd_state"):
            return {**_provenance(cfg), "note": "driver does not expose MSD storage inventory"}
        out = {**_provenance(cfg), "msd": kvm.get_msd_state()}
        pattern = getattr(kvm, "virtual_media_host_pattern", None)
        if pattern:
            out["host_visible_as"] = pattern
        return out


@mcp.tool(annotations=_DESTRUCTIVE)
async def mount_iso(
    ctx: Context,
    source: str,
    name: str | None = None,
    usb: bool = False,
    confirm: bool = False,
    profile: str | None = None,
) -> dict:
    """Mount an ISO as virtual media on the host. DESTRUCTIVE (media).

    ``source`` is a local path or an ``http(s)://`` URL; ``usb=true`` attaches as a
    USB flash drive instead of a CD-ROM. Needs ``KVM_PILOT_MCP_ALLOW_MEDIA`` +
    per-invocation approval. Mounting bumps the frame generation, so a mouse click
    planned against the pre-mount screen is invalidated.
    """
    return await _act(
        ctx, profile, tool="mount_iso", effect=EffectClass.MEDIA, op="msd.connect",
        transport="msd", args={"source": source, "name": name, "usb": usb}, confirm=confirm,
        run=lambda kvm: cast("VirtualMedia", kvm).mount_iso(source, image_name=name, cdrom=not usb),
        detail=f"mounted {source!r}",
        capability=Capability.VIRTUAL_MEDIA,
    )


@mcp.tool(annotations=_DESTRUCTIVE)
async def eject(ctx: Context, confirm: bool = False, profile: str | None = None) -> dict:
    """Detach virtual media (the inverse of ``mount_iso``). DESTRUCTIVE (media).

    Needs ``KVM_PILOT_MCP_ALLOW_MEDIA`` + per-invocation approval.
    """
    return await _act(
        ctx, profile, tool="eject", effect=EffectClass.MEDIA, op="msd.disconnect",
        transport="msd", args={}, confirm=confirm,
        run=lambda kvm: cast("VirtualMedia", kvm).msd_disconnect(),
        detail="ejected virtual media",
        capability=Capability.VIRTUAL_MEDIA,
    )


@mcp.tool(annotations=_READ_ONLY)
def ssh_reachable(profile: str | None = None, host: str | None = None) -> dict:
    """Is the managed host's OS reachable over SSH? (read-only, in-band).

    Targets the host *behind* the KVM (its own ``ssh_host`` / `KVM_PILOT_SSH_HOST`),
    a different machine from the KVM appliance. Use this to prefer remote recovery
    before asking a user to physically intervene.

    ``host`` overrides the profile/env ``ssh_host`` at runtime — e.g. an install-time
    DHCP address the profile can't know until the target boots.
    """
    cfg = resolve_host(profile or os.environ.get("KVM_PILOT_PROFILE"))
    if host:
        cfg.ssh_host = host
    from kvm_pilot.ssh import SSHChannel

    try:
        ch = SSHChannel.from_config(cfg)
    except CapabilityError as exc:
        raise ToolError(str(exc)) from exc
    return {
        **_provenance(cfg),
        "target": ch.target,
        "port": ch.port,
        "reachable": ch.ssh_reachable(),
    }


@mcp.tool(annotations=_DESTRUCTIVE)
def ssh_exec(
    command: str, confirm: bool = False, profile: str | None = None, host: str | None = None
) -> dict:
    """Run a command on the managed host's OS over SSH. DESTRUCTIVE / in-band.

    Disabled unless the server operator set `KVM_PILOT_MCP_ALLOW_SSH` in the
    server's own environment. ``confirm=true`` is required as a second factor.
    ``host`` overrides the profile/env ``ssh_host`` at runtime (e.g. a discovered
    install-time DHCP address).
    """
    if not _env_flag("KVM_PILOT_MCP_ALLOW_SSH"):
        raise ToolError(
            "ssh_exec is disabled on this server. Only the human operator can enable "
            "it, by setting the SSH-enable environment variable (documented in the "
            "server's README.md) in the MCP server's own environment before starting "
            "it. It cannot be enabled from within an agent session."
        )
    if not confirm:
        raise ToolError("ssh_exec was not confirmed")
    cfg = resolve_host(profile or os.environ.get("KVM_PILOT_PROFILE"))
    if host:
        cfg.ssh_host = host
    from kvm_pilot.ssh import SSHChannel

    try:
        ch = SSHChannel.from_config(cfg, confirm=allow_all, dry_run=_dry_run())
    except CapabilityError as exc:
        raise ToolError(str(exc)) from exc
    return {**_provenance(cfg), **ch.ssh_exec(command)}


@mcp.tool(annotations=_READ_ONLY)
def appliance_status(profile: str | None = None) -> dict:
    """Read-only diagnostics from the KVM APPLIANCE's own OS over SSH.

    Targets the KVM appliance itself (its ``appliance_ssh`` channel), NOT the
    managed target. Reports the 1-minute load and the RV1126 video-pipeline threads
    in D-state. NOTE: on these units load sits at ~10 even when perfectly idle
    (the driver parks those threads in D-state), so it is NOT a health signal on
    its own — use the ``healthcheck`` ``encoder-wedge`` finding for the real tell.
    """
    cfg = resolve_host(profile or os.environ.get("KVM_PILOT_PROFILE"))
    from kvm_pilot.ssh import ApplianceChannel

    try:
        ch = ApplianceChannel.from_config(cfg)
    except CapabilityError as exc:
        raise ToolError(str(exc)) from exc
    return {
        **_provenance(cfg),
        "appliance": ch.target,
        "loadavg_1m": ch.loadavg(),
        "d_state_video_threads": ch.d_state_video_threads(),
        "note": "loadavg is ~= the D-state thread count even when idle; not a health signal.",
    }


@mcp.tool(annotations=_DESTRUCTIVE)
def appliance_reboot(confirm: bool = False, profile: str | None = None) -> dict:
    """Reboot the KVM APPLIANCE (not the target) to clear a wedged encoder. DESTRUCTIVE.

    Recovers the RV1126 encoder wedge (the only fix — the stuck threads are
    unkillable kernel threads). Drops all KVM control for ~60s; the target's power
    is untouched. Disabled unless the operator set ``KVM_PILOT_MCP_ALLOW_APPLIANCE``
    in the server's own environment; ``confirm=true`` is required as a second factor.
    There is no out-of-band power to the appliance, so use this deliberately, never
    in an automated loop.
    """
    if not _env_flag("KVM_PILOT_MCP_ALLOW_APPLIANCE"):
        raise ToolError(
            "appliance reboot is disabled on this server. Only the human operator can "
            "enable it, by setting the appliance-enable environment variable (documented "
            "in the server's README.md) in the MCP server's own environment before "
            "starting it. It cannot be enabled from within an agent session."
        )
    if not confirm:
        raise ToolError("appliance_reboot was not confirmed")
    cfg = resolve_host(profile or os.environ.get("KVM_PILOT_PROFILE"))
    from kvm_pilot.ssh import ApplianceChannel

    try:
        ch = ApplianceChannel.from_config(cfg, confirm=allow_all, dry_run=_dry_run())
    except CapabilityError as exc:
        raise ToolError(str(exc)) from exc
    return {**_provenance(cfg), **ch.reboot()}


@mcp.tool(annotations=_READ_ONLY)
def access_paths(profile: str | None = None) -> dict:
    """Which INDEPENDENT recovery paths are live for the device — the lockout view.

    Rolls up the REST API, appliance-SSH, target-SSH, out-of-band power, and
    console-HID paths, each labeled by its failure *domain* so redundancy is not
    oversold: several live paths that all ride the same appliance are ONE
    independent domain. `summary.out_of_band_live=false` means every path shares
    the appliance's fate — a fully hung box can't be recovered remotely.
    """
    with _driver(profile, confirm=deny_all, capability=Capability.SYSTEM_INFO) as (cfg, kvm):
        from kvm_pilot.health import access_paths as _access_paths

        return {**_provenance(cfg), **_access_paths(kvm)}


@mcp.tool(annotations=_DESTRUCTIVE)
async def file_firmware_report(
    ctx: Context,
    confirm: bool = False,
    repo: str | None = None,
    source: str | None = None,
    dry_run: bool = False,
    profile: str | None = None,
) -> dict:
    """Contribute the device's firmware currency to the registry SSoT — files the
    "Latest known release" report as a GitHub issue when the registry is behind
    (the MCP twin of CLI ``firmware-check``, #189/#190). EXTERNAL WRITE.

    The read/reconcile part always runs; when the registry already reflects the
    device-reported latest there is nothing to file and the result says so. The
    filing itself writes OUTSIDE the managed device (a public GitHub issue via
    the ``gh`` CLI), so it is gated as its own ``external_write`` effect class:
    **disabled unless** the operator set ``KVM_PILOT_MCP_ALLOW_EXTERNAL_WRITE``
    in the server's own environment, plus the usual per-invocation approval
    (elicitation, or ``confirm=true`` under standing policy). ``dry_run=true``
    returns the exact issue title/body without sending anything. A missing or
    unauthenticated ``gh`` comes back as a graceful ``filed=false`` reason.
    """
    from kvm_pilot.firmware_registry import (
        UPSTREAM_REPO,
        check_currency,
    )
    from kvm_pilot.firmware_registry import (
        file_firmware_report as _file_report,
    )

    repo = repo or UPSTREAM_REPO
    with _driver(profile, confirm=deny_all, capability=Capability.SYSTEM_INFO) as (cfg, kvm):
        fw, upd, submission = check_currency(kvm)
        base = {**_provenance(cfg), "vendor": (fw.get("vendor") or "").strip(),
                "product": fw.get("product") or "", "installed": fw.get("version"),
                "registry_behind": submission is not None}
        if submission is None:
            reason = ("registry already reflects the device-reported latest"
                      if upd and upd.get("latest")
                      else "device does not self-report an available-update check")
            return {**base, "filed": False, "reason": reason}
        inv = act.new_invocation(
            host=cfg.host, profile=profile, tool="file_firmware_report",
            effect=EffectClass.EXTERNAL_WRITE, op="report.file_firmware",
            transport="gh-cli", args={"repo": repo, "submission": submission},
            dry_run=_dry_run() or dry_run,
        )
        approval = await act.approve_or_deny(ctx, inv, confirm=confirm)
        if not approval.approved:
            return {**base, **act.result(inv, approval)}
        receipt, denied = _consume_receipt(inv, approval)
        if denied is not None:
            return {**base, **denied}
        try:
            outcome = await anyio.to_thread.run_sync(
                lambda: _file_report(submission, repo=repo, source=source, dry_run=inv.dry_run)
            )
        except Exception as exc:
            act.audit_dispatch_error(inv, receipt, exc)
            raise
        return {**base, **act.result(inv, approval, receipt=receipt), "report": outcome,
                "filed": outcome.get("filed", False)}


@mcp.tool(annotations=_READ_ONLY)
def ssh_discover(cidr: str, confirm: bool = False, port: int = 22) -> dict:
    """Scan a CIDR for hosts with an open SSH port. RISKY — opt-in.

    An active network scan: noisy, and only acceptable on networks the user owns or
    is authorized to probe. Use it ONLY to help find a target whose address the user
    doesn't know, after they confirm the range — never by default. ``confirm=true``
    is required to acknowledge the scan.
    """
    if not confirm:
        raise ToolError(
            "ssh_discover was not confirmed. A network scan is risky/noisy — only run "
            "it on networks the user owns, after they confirm the range."
        )
    from kvm_pilot.ssh import discover_ssh_hosts

    try:
        candidates = discover_ssh_hosts(cidr, port=port)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return {"cidr": cidr, "port": port, "candidates": candidates}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
