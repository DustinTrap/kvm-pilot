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
    ``ssh_reachable``, ``ssh_discover``, ``doctrine``) run with a
    deny-all confirm callback and carry a ``readOnlyHint`` tool annotation
    (``ssh_discover`` additionally requires ``confirm=true`` — an active
    network scan is read-only but not harmless).
  * The state-changing tools (``power``, the HID act tools ``type_text`` /
    ``press_key`` / ``send_shortcut`` / ``ctrl_alt_delete`` / ``mouse`` /
    ``calibrate_mouse``, the
    media tools ``mount_iso`` / ``eject``, ``ssh_exec``, and the
    external-write tool ``file_firmware_report``) carry per-tool annotations
    (#195: ``destructiveHint`` for the irreversible ones; the reversible media
    and external-write tools are annotated non-destructive but stay gated all
    the same) and are DISABLED until the operator opts the tool's
    *effect class* in via its effect gate in the server's own environment
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
from datetime import UTC, datetime
from importlib.resources import files
from typing import TYPE_CHECKING, Literal, cast

import anyio
from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from kvm_pilot import resolve_host
from kvm_pilot.calibrate import (
    CalibrationError,
    maybe_apply,
    run_calibration,
    save_calibration,
)
from kvm_pilot.config import HostConfig
from kvm_pilot.drivers import Capability, KVMDriver, make_driver_from_config
from kvm_pilot.errors import (
    CapabilityError,
    KVMPilotError,
    UnavailableError,
    VisionError,
)
from kvm_pilot.errors import TimeoutError as KVMTimeoutError
from kvm_pilot.health import RECOVERY_ORDER
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
    from kvm_pilot.drivers.base import (
        HID,
        BootConfig,
        Logs,
        Power,
        SystemInfo,
        Video,
        VirtualMedia,
    )

mcp = FastMCP("kvm-pilot")
_log = logging.getLogger("kvm_pilot.mcp")

# Tool annotations (#195): every tool declares all four hints explicitly,
# because clients act on them (auto-approval and parallel dispatch of read-only
# tools, confirmation UI keyed off destructiveHint) and the spec defaults are
# punitive — an unset destructiveHint reads as True and an unset openWorldHint
# reads as "reaches the internet". Hints advise well-behaved clients; the
# ALLOW_* effect gates in act.py remain the security boundary.
#
#   _READ            pure reads against the device/ledger on the operator's own
#                    network.
#   _READ_VISION     read-only, but the server-side vision backend may be a
#                    cloud VLM (openWorldHint=True is the worst case; with a
#                    local backend nothing leaves the network). wait_for_state
#                    is additionally time-dependent, so its variant drops the
#                    idempotent hint.
#   _DESTRUCTIVE     state-changing and not safely repeatable (a second reset
#                    reboots again; a second keystroke types again).
#   _REVERSIBLE_*    state-changing but undoable and convergent (eject of
#                    already-ejected media is a no-op; re-mounting the same
#                    image yields the same attached state). Still gated by
#                    effect class + per-invocation approval — destructiveHint
#                    speaks to irreversibility, not to whether we gate it.
#                    The _REMOTE variant may leave the operator's network
#                    (mount by URL; filing a GitHub issue — which dedupes
#                    against existing issues, hence idempotent).
_READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_READ_VISION = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
_READ_VISION_WAIT = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True
)
_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)
_REVERSIBLE_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_REVERSIBLE_WRITE_REMOTE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
)

_POWER_ACTIONS = {
    "on": "power_on",
    "off": "power_off",
    "off-hard": "power_off_hard",
    "reset": "reset_hard",
}


# Single source of truth for the effect-gate env flags (shared with the act layer).
_env_flag = act.env_flag


def _dry_run() -> bool:
    return _env_flag("KVM_PILOT_MCP_DRY_RUN")


def _read_only_mode() -> bool:
    """Least-privilege launch posture (#196): destructive tools don't exist.

    Distinct from dry-run (a rehearsal mode — destructive tools stay registered
    and log what they *would* send). Read-only wins over ``ALLOW_*`` and over
    dry-run; the trust ladder is READ_ONLY → DRY_RUN → open effect gates one at
    a time.
    """
    return _env_flag("KVM_PILOT_MCP_READ_ONLY")


# ssh_discover is annotated read-only (it changes nothing) but it is an active
# network scan — it has no place in the least-privilege intake/demo posture.
_READ_ONLY_MODE_EXCLUDED = {"ssh_discover"}


def _apply_read_only_mode() -> None:
    """Under ``KVM_PILOT_MCP_READ_ONLY``, unregister every non-read-only tool.

    Annotation-driven, like kubernetes-mcp-server's ``--read-only``: a tool
    survives only if it declares ``readOnlyHint=True`` (and isn't excluded
    above). This is the visible layer; ``gate_enabled`` force-closes every
    effect class and ``_driver`` builds drivers with a deny-all confirm as the
    independent layers beneath it, so a filter bypass fails closed rather than
    mutating (the CVE-2026-46519 lesson: tool filtering alone is not
    enforcement).
    """
    if not _read_only_mode():
        return
    for tool in list(mcp._tool_manager.list_tools()):
        ann = tool.annotations
        if ann is None or ann.readOnlyHint is not True or tool.name in _READ_ONLY_MODE_EXCLUDED:
            mcp.remove_tool(tool.name)


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
    if _read_only_mode():
        # Defense in depth (#196): in read-only mode every driver denies all
        # destructive calls at the safety layer, so even a tool that slipped
        # past the registration filter cannot mutate the target.
        confirm = deny_all
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
    if _read_only_mode():
        out["read_only"] = True
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


@mcp.tool(annotations=_READ)
def info(profile: str | None = None) -> dict:
    """Return device / system info (read-only)."""
    with _driver(profile, confirm=deny_all, capability=Capability.SYSTEM_INFO) as (cfg, kvm):
        return {**_provenance(cfg), "info": cast("SystemInfo", kvm).get_info()}


@mcp.tool(annotations=_READ)
def healthcheck(profile: str | None = None) -> dict:
    """Audit the device's readiness/recovery, security posture, and firmware (#80).

    Read-only. Returns per-check findings with a tiered severity; a ``CRITICAL``
    (e.g. no out-of-band reset path) is what should gate a subsequent destructive
    op. The most valuable finding is ``recovery-path`` — whether a hung guest can
    be reset at all when the KVM is remote. Served through the preflight cache
    (#225): stable posture may come from the last assessment, and a firmware
    change since then adds a ``firmware-delta`` finding of what cleared/regressed.
    """
    # preflight=False: this tool IS the audit — the implicit driver-build
    # preflight would run the same checks a second time.
    with _driver(
        profile, confirm=deny_all, capability=Capability.SYSTEM_INFO, preflight=False
    ) as (cfg, kvm):
        from kvm_pilot.health import HealthCache, note_session_audited, preflight

        # enforce=False: an explicitly requested health *report* must report,
        # never raise — enforcement stays on the destructive paths.
        report = preflight(kvm, cache=HealthCache(), enforce=False)
        note_session_audited(kvm)
        return {**_provenance(cfg), **report.to_dict()}


@mcp.tool(annotations=_READ)
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


@mcp.tool(annotations=_READ)
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


# -- doctrine (#222): mid-session re-anchor on the bundled operating doctrine --


def _doctrine_topics() -> dict:
    """Topic name -> Traversable for SKILL.md (``core``) and each reference."""
    root = files("kvm_pilot").joinpath("skill")
    topics = {"core": root.joinpath("SKILL.md")}
    for entry in root.joinpath("references").iterdir():
        if entry.name.endswith(".md"):
            topics[entry.name[:-3]] = entry
    return topics


def _md_title(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


@mcp.tool(annotations=_READ)
def doctrine(topic: str | None = None) -> dict:
    """Re-serve the bundled operating doctrine (read-only; offline, no device I/O).

    The skill's playbooks ship inside this package; this tool re-serves them so
    a session that never loaded the skill file — or has long since compacted it
    away — can re-anchor on the written doctrine instead of a faded memory of
    it. Call with no ``topic`` to list the topics; call with one for that
    playbook's full text. Read ``recovery`` the moment a host goes dark or a
    snapshot fails, and ``interfaces`` before picking how to do an action you
    haven't done this session.
    """
    topics = _doctrine_topics()
    if topic is None:
        return {
            "topics": {
                name: _md_title(entry.read_text(encoding="utf-8"))
                for name, entry in sorted(topics.items())
            },
            "note": (
                "call again with topic=<name> for the full text; 'recovery' is "
                "the one to read when a host is dark or wedged"
            ),
        }
    entry = topics.get(topic)
    if entry is None:
        raise ToolError(
            f"unknown doctrine topic {topic!r}. Valid topics: {', '.join(sorted(topics))}"
        )
    return {"topic": topic, "text": entry.read_text(encoding="utf-8")}


@mcp.resource("kvm-pilot://doctrine/{topic}")
def doctrine_resource(topic: str) -> str:
    """One doctrine playbook as a readable MCP resource (#231).

    Same bytes as the `doctrine` tool — the resource form lets resource-capable
    clients list/read/@-mention the playbooks without a tool round-trip. The
    tool stays for clients without resource support and for compacted sessions
    that only remember tools.
    """
    entry = _doctrine_topics().get(topic)
    if entry is None:
        raise ValueError(f"unknown doctrine topic {topic!r}")
    return entry.read_text(encoding="utf-8")


def _cached_firmware(cfg: HostConfig) -> str | None:
    """Last-assessed firmware from the on-disk HealthCache — offline, best-effort."""
    try:
        from kvm_pilot.health import HealthCache

        return HealthCache().last_assessed(f"{cfg.driver}@{cfg.host}")
    except Exception:  # noqa: BLE001 - a posture read must never fail the tool
        return None


@mcp.tool(annotations=_READ)
def session(profile: str | None = None) -> dict:
    """Report this server's current operating posture (read-only; offline, no
    device I/O — answers even when the device is down).

    Call this after a context compaction, when resuming a long flow, or before
    planning act calls: it names the target, dry-run/read-only state, which
    effect gates are open (by class name only — opening one is operator-only,
    out of band), the approval posture, the recent act journal, and the last
    ``wait_for_state`` result for the target. All journal/wait state is
    in-memory: a server restart empties it (and voids receipts and frame refs),
    so an empty journal after a restart is expected, not evidence nothing
    happened. Pair with ``healthcheck`` for device health and ``doctrine`` for
    the operating playbooks.
    """
    out: dict = {
        "server": {
            "read_only": _read_only_mode(),
            "dry_run": _dry_run(),
            "approval_posture": act.approval_posture(),
            "profile_allowlist": act.allowlist_names(),
        },
        "gates": act.gate_summary(),
        "recent_acts": act.journal_tail(),
    }
    try:
        act.enforce_allowlist(profile)
        cfg = resolve_host(profile or os.environ.get("KVM_PILOT_PROFILE"))
        out["target"] = {
            "profile": profile or os.environ.get("KVM_PILOT_PROFILE"),
            "host": cfg.host,
            "driver": cfg.driver,
            "firmware_last_assessed": _cached_firmware(cfg),
            "frame_generation": act.generation(cfg.host),
            "last_wait": act.last_wait(cfg.host),
        }
    except Exception as exc:  # noqa: BLE001 - a re-anchor tool never refuses
        out["target"] = {"error": str(exc)}
    return out


@mcp.tool(annotations=_READ)
def power_state(profile: str | None = None) -> dict:
    """Return whether the host is powered on, plus ATX detail where the driver has it
    (read-only)."""
    with _driver(profile, confirm=deny_all, capability=Capability.POWER) as (cfg, kvm):
        state = {**_provenance(cfg), "powered_on": cast("Power", kvm).is_powered_on()}
        # ATX detail is PiKVM-family only; Redfish/fake answer via is_powered_on().
        if hasattr(kvm, "get_atx_state"):
            state["atx"] = kvm.get_atx_state()
        return state


@mcp.tool(annotations=_READ)
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


@mcp.tool(annotations=_READ)
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
        try:
            img = cast("Video", kvm).snapshot()
        except UnavailableError as exc:
            raise _video_unavailable_error(exc) from exc
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


def _video_unavailable_error(exc: KVMPilotError) -> ToolError:
    """Decorate a video-subsystem 503 with the next step (#222).

    The failure often arrives long after the model's last doctrine read, so
    the message carries the recovery ladder instead of assuming it's
    remembered. Shared by every tool that captures a frame.
    """
    return ToolError(
        f"snapshot unavailable: {exc} — the video subsystem (streamer/encoder) "
        "is down, which does not by itself mean the host is. Check `logs` for "
        "the named fault first. If the host itself is dark, the remote-first "
        f"recovery order is: {RECOVERY_ORDER}; the `doctrine` tool "
        "(topic 'recovery') has the full playbook."
    )


@mcp.tool(annotations=_READ_VISION)
def classify_screen(hint: str = "", profile: str | None = None):
    """Classify the current screen's boot/run phase (read-only).

    Uses the server-side vision backend when configured; cheap on-device gates
    (power-off, no-signal, boot-progress, OCR rules) resolve with no
    credentials at all. Return shapes:
      * server-side / cheap-gate → a dict with ``mode="server"`` + phase fields.
      * no server vision → caller-side fallback, a ``[json_text, Image]`` list:
        classify the image yourself against the ``system_prompt`` / ``phases``
        in the JSON block.
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
    except UnavailableError as e:
        raise _video_unavailable_error(e) from e
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


@mcp.tool(annotations=_READ_VISION_WAIT)
async def wait_for_state(
    ctx: Context,
    phase: str,
    timeout: float = 60.0,
    hint: str = "",
    profile: str | None = None,
) -> dict:
    """Wait (bounded) until the screen reaches a boot/run phase (read-only).

    Server-side twin of CLI `watch`: polls cheap power/signal/boot-progress
    gates, then server-side vision, until `phase` (a `classify_screen` token;
    unknown tokens fail fast with the valid list) is observed. `timeout` is
    seconds, capped server-side at 300 — chain calls for longer waits. Success
    returns phase/confidence plus a `frame_ref` to pass to `mouse` as
    `observed_frame_ref`; a timeout returns `reached=false` with the last
    observed state (never a hang, never a raised error). With no server-side
    vision credentials only cheap-gate phases are waitable; others fail fast
    pointing at `classify_screen` polling. Holds the driver open up to
    `timeout` s. Details: `doctrine` topic 'interfaces'.
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
            started_at = datetime.now(UTC).isoformat(timespec="seconds")

            def _breadcrumb(reached: bool) -> None:
                # #223: the resume anchor a compacted session asks `session` for.
                act.note_wait(cfg.host, {
                    "phase": phase, "reached": reached, "timeout_s": timeout,
                    "started_at": started_at,
                    "ended_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "elapsed_s": round(time.monotonic() - start, 1),
                    "last_observed": last[0].to_dict()["phase"] if last else None,
                })

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
                _breadcrumb(False)
                out = {
                    **_provenance(cfg), "reached": False, "waited_for": phase,
                    "timeout_s": timeout, "detail": str(exc),
                }
                if last:
                    out["last"] = last[0].to_dict()
                return out
            _breadcrumb(True)
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
async def power(
    ctx: Context,
    action: Literal["on", "off", "off-hard", "reset"],
    confirm: bool = False,
    profile: str | None = None,
) -> dict:
    """Change host power state. DESTRUCTIVE.

    Gated by the power effect gate + per-invocation approval (elicitation, or
    ``confirm=true`` under standing policy); denials come back through the same
    path with a typed ``outcome`` (#234). The result carries an honest effect
    report (#168): ``verified`` is true/false when the driver has a trustworthy
    power signal (Redfish PowerState, a wired ATX LED), and ``null`` — with the
    reason and what to do instead — when it doesn't. A power action also
    invalidates prior ``snapshot`` frame refs (generation bump), so a stale
    mouse click can't land on the post-reboot screen.
    """

    def _run(cfg: HostConfig, kvm: KVMDriver) -> dict:
        getattr(kvm, _POWER_ACTIONS[action])()
        if _dry_run():
            return {
                "requested": action, "verified": None, "observed": None,
                "note": (
                    f"power {action}: DRY-RUN — logged only, nothing was sent to "
                    f"host '{cfg.host}' ({cfg.driver})"
                ),
            }
        verified, observed, note = _verify_power(kvm, action)
        return {
            "requested": action, "verified": verified, "observed": observed,
            "note": note,
            "detail": f"power {action}: requested on host '{cfg.host}' ({cfg.driver})",
        }

    # Graceful off/on are soft; forced off and reset are hard. Both ride the
    # power effect gate — the distinction is recorded on the receipt.
    effect = (
        EffectClass.POWER_HARD if action in ("off-hard", "reset") else EffectClass.POWER_SOFT
    )
    return await _act(
        ctx, profile, tool="power", effect=effect, op=f"power.{action}",
        transport="atx.power", args={"action": action}, confirm=confirm,
        run=_run, detail=f"power {action}",
        capability=Capability.POWER, enforce_health=True,
    )


@mcp.tool(annotations=_READ)
def boot_options(profile: str | None = None) -> dict:
    """Show the host's current boot override (Redfish BootSourceOverride) — read-only.

    Reports ``enabled`` (Disabled/Once/Continuous), the normalized ``target``, the
    ``mode`` (UEFI/Legacy, or null if the BMC doesn't expose it), and the
    ``allowable`` targets the BMC advertises — so an actuator knows what
    ``set_boot_device`` values this box will accept before trying one.
    """
    with _driver(profile, confirm=deny_all, capability=Capability.BOOT_CONFIG) as (cfg, kvm):
        return {**_provenance(cfg), "boot": cast("BootConfig", kvm).get_boot_options()}


@mcp.tool(annotations=_DESTRUCTIVE)
async def set_boot_device(
    ctx: Context,
    device: Literal["pxe", "cd", "hdd", "usb", "bios", "diag", "none"],
    once: bool = True,
    uefi: bool = True,
    confirm: bool = False,
    profile: str | None = None,
) -> dict:
    """Set the next-boot (or persistent) boot device via BootSourceOverride. CONFIG MUTATION.

    Gated by the config effect gate + per-invocation approval; denials come
    back through the same path with a typed ``outcome`` (#234). ``none`` clears
    the override; ``once=false`` makes it persistent; ``uefi=false`` selects
    legacy BIOS mode where the target exposes it. A target the BMC doesn't
    advertise fails fast (call ``boot_options`` first to see ``allowable``).
    """
    return await _act(
        ctx, profile, tool="set_boot_device", effect=EffectClass.CONFIG_MUTATION,
        op=f"boot.{device}", transport="bmc.boot_override",
        args={"device": device, "once": once, "uefi": uefi}, confirm=confirm,
        run=lambda _cfg, kvm: {
            "requested": device, "once": once, "uefi": uefi,
            "boot": cast("BootConfig", kvm).set_boot_device(device, once=once, uefi=uefi),
        },
        detail=f"boot override {device} ({'once' if once else 'persistent'})",
        capability=Capability.BOOT_CONFIG, enforce_health=True,
    )


@mcp.tool(annotations=_DESTRUCTIVE)
async def amt_enable(
    ctx: Context,
    feature: Literal["sol", "kvm"],
    consent_off: bool = False,
    confirm: bool = False,
    profile: str | None = None,
) -> dict:
    """Enable an Intel AMT redirection listener over WS-Man (Intel AMT/vPro only). CONFIG MUTATION.

    ``feature='sol'`` opens the SOL/IDE-R listener (16994); ``feature='kvm'`` opens
    KVM redirection (5900) and sets the RFB password. Gated by the config effect
    gate + per-invocation approval (typed same-path denials, #234).
    ``consent_off=true`` (KVM only) DISABLES the on-screen user-consent prompt —
    a surveillance escalation — and additionally requires the dedicated
    ``KVM_PILOT_MCP_ALLOW_CONSENT_OFF`` operator gate (see README.md); leaving
    it false keeps the prompt.
    """
    # Usage/modifier-gate validation stays a fast ToolError — it precedes the
    # act flow (the consent-off gate is a modifier on top of the config gate,
    # not an effect class of its own; stay-mum per #224).
    if consent_off:
        if feature != "kvm":
            raise ToolError("consent_off applies only to feature='kvm'")
        if not _env_flag("KVM_PILOT_MCP_ALLOW_CONSENT_OFF"):
            raise ToolError(
                "disabling AMT user-consent is disabled on this server. It lets anyone with "
                "the AMT credentials watch and control the console with NO on-screen prompt, "
                "so it needs its own dedicated operator gate beyond the config gate "
                "(documented in the server's README.md). It cannot be enabled from within "
                "an agent session."
            )

    def _run(_cfg: HostConfig, kvm: KVMDriver) -> dict:
        fn = getattr(kvm, "enable_sol" if feature == "sol" else "enable_kvm", None)
        if fn is None:
            raise ToolError("this driver has no AMT feature enablement (use the amt driver)")
        if feature == "sol":
            fn()
        else:
            fn(require_consent=not consent_off)
        return {
            "feature": feature,
            "port": 16994 if feature == "sol" else 5900,
            "consent": "off" if consent_off else "on",
        }

    return await _act(
        ctx, profile, tool="amt_enable", effect=EffectClass.CONFIG_MUTATION,
        op=f"amt.enable_{feature}", transport="wsman.config",
        args={"feature": feature, "consent_off": consent_off}, confirm=confirm,
        run=_run, detail=f"AMT {feature} listener enabled",
        capability=None, enforce_health=True,
    )


@mcp.tool(annotations=_DESTRUCTIVE)
async def wake(
    ctx: Context,
    mac: str | None = None,
    broadcast: str | None = None,
    count: int = 3,
    confirm: bool = False,
    profile: str | None = None,
) -> dict:
    """Send a Wake-on-LAN magic packet to power the host on. POWER (soft).

    Gated by the power effect gate + per-invocation approval (typed same-path
    denials, #234). ``mac`` defaults to the profile's ``mac``; ``broadcast`` to
    its ``wol_broadcast``. No KVM driver is contacted — WoL is a broadcast sent
    from the server's own host onto the target's L2 segment.
    """
    cfg = resolve_host(profile or os.environ.get("KVM_PILOT_PROFILE"))
    target = mac or cfg.mac
    if not target:
        raise ToolError("no MAC — pass mac=, or set 'mac' in the host profile")
    bc = broadcast or cfg.wol_broadcast

    def _run() -> dict:
        from kvm_pilot import wol

        if not _dry_run():
            wol.send_magic_packet(target, broadcast=bc, count=count)
        return {"requested_mac": target, "broadcast": bc, "count": count}

    return await _channel_act(
        ctx, profile, cfg, tool="wake", effect=EffectClass.POWER_SOFT,
        op="wol.wake", transport="net.wol", args={"mac": target}, confirm=confirm,
        run=_run, detail=f"WoL magic packet x{count} to {target}",
    )


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


async def _channel_act(
    ctx: Context,
    profile: str | None,
    cfg: HostConfig,
    *,
    tool: str,
    effect: EffectClass,
    op: str,
    transport: str,
    args: dict,
    confirm: bool,
    run: Callable[[], object],
    detail: str,
) -> dict:
    """Act flow for tools that dispatch over a channel (WoL packet, SSH), not a
    device driver (#234): same two-guarantee approval, receipt, audit terminals,
    and same-path typed denials as ``_act``, minus the driver build. The caller
    resolves ``cfg`` (and validates usage) first; the allowlist is enforced here.
    """
    act.enforce_allowlist(profile)
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
        extra = run()
    except Exception as exc:
        act.audit_dispatch_error(inv, receipt, exc)
        raise
    if not inv.dry_run and act.bumps_generation(inv.effect):
        act.bump_generation(cfg.host)
    return {**_provenance(cfg), **act.result(
        inv, approval, detail=detail, receipt=receipt,
        extra=extra if isinstance(extra, dict) else None,
    )}


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
    run: Callable[[HostConfig, KVMDriver], object],
    detail: str,
    capability: Capability | None = Capability.HID,
    enforce_health: bool = False,
) -> dict:
    """Shared act-tool flow (#61): resolve+gate the driver, run the two-guarantee
    approval, execute on approval, and return the receipt. Denials come back
    through the same path (a result with ``approved=False`` + a reason), never a
    raised error, so the agent can recover. Dry-run is handled by the driver's own
    SafetyPolicy, so ``run`` becomes a no-op and the receipt still records intent.
    ``run`` may return a dict of tool-specific result fields (merged into the
    result, #234); any other return is ignored.
    """
    with _driver(
        profile, confirm=allow_all, capability=capability, enforce_health=enforce_health
    ) as (cfg, kvm):
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
            extra = run(cfg, kvm)
        except Exception as exc:
            act.audit_dispatch_error(inv, receipt, exc)
            raise
        # A media/power effect changes the screen enough to invalidate prior
        # observations — bump the frame generation so a stale mouse ref won't match.
        if not inv.dry_run and act.bumps_generation(inv.effect):
            act.bump_generation(cfg.host)
        return {**_provenance(cfg), **act.result(
            inv, approval, detail=detail, receipt=receipt,
            extra=extra if isinstance(extra, dict) else None,
        )}


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
        run=lambda _cfg, kvm: cast("HID", kvm).type_text(text),
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
        run=lambda _cfg, kvm: cast("HID", kvm).press_key(key),
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
        run=lambda _cfg, kvm: cast("HID", kvm).send_shortcut(keys),
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
        run=lambda _cfg, kvm: cast("HID", kvm).send_shortcut("ControlLeft,AltLeft,Delete"),
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


def _run_mouse(
    kvm: KVMDriver, x: float, y: float, coord_space: str, button: str | None,
    host: str | None = None,
) -> bool:
    """Execute the move(+click); returns whether a stored calibration was applied.

    Percent coords route through the per-host correction when one exists for
    the current resolution (#128) — pixel/raw coords are the caller saying
    "exactly here" and are never adjusted.
    """
    calibrated = False
    hid = cast("HID", kvm)
    if coord_space == "percent":
        if host is not None:
            x, y, calibrated = maybe_apply(host, kvm, x, y)
        # Prefer the driver's own percent mapping when it has one (AMT maps onto
        # its real framebuffer pixels; kvmd maps onto its centered range). Falling
        # back to pct_to_kvmd would feed a pixel-native driver (AMT) a negative
        # kvmd value and throw — #181/AMT first-class.
        move_pct = getattr(kvm, "mouse_move_percent", None)
        if move_pct is not None:
            move_pct(x, y)
        else:
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
    return calibrated


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

    A **click** must carry the ``observed_frame_ref`` it was planned against
    (from a prior ``snapshot``); it is refused — re-``snapshot`` and retry — if
    the host rebooted/swapped media since, the observation is older than
    ``KVM_PILOT_MCP_FRAME_MAX_AGE`` (60s default, #141), or the ref wasn't
    issued by this server. Move-only (``button`` omitted) needs no ref.
    ``coord_space``: ``percent`` (0.0-1.0, default — survives resolution
    changes), ``pixel``, or ``raw`` kvmd. Gated by ``KVM_PILOT_MCP_ALLOW_HID``
    + per-invocation approval.
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
        calibrated = False
        if not inv.dry_run:
            try:
                calibrated = _run_mouse(kvm, x, y, coord_space, button, host=cfg.host)
            except Exception as exc:
                act.audit_dispatch_error(inv, receipt, exc)
                raise
        detail = f"moved to ({x}, {y}) [{coord_space}]" + (f" + {button} click" if button else "")
        return {
            **_provenance(cfg),
            **act.result(inv, approval, detail=detail,
                         extra={"coord_space": coord_space, "calibrated": calibrated},
                         receipt=receipt),
        }


@mcp.tool(annotations=_REVERSIBLE_WRITE)
async def calibrate_mouse(
    ctx: Context,
    confirm: bool = False,
    tolerance: float = 0.02,
    profile: str | None = None,
) -> dict:
    """Measure and store this host's mouse commanded→observed correction (#128).

    Fixes "clicks where the button should be and misses". Pointer moves only —
    no clicks, no keystrokes — but it visibly moves the live cursor ~10-30s,
    so it is gated like HID input (``KVM_PILOT_MCP_ALLOW_HID``; one approval
    covers the whole run). Preconditions: live video signal, a **static**
    screen, a visible cursor, Pillow on the server
    (``pip install 'kvm-pilot[calibrate]'``). Afterwards ``mouse`` percent
    coords apply it transparently (``calibrated: true``); stored per (host,
    capture resolution) — a resolution change makes it stale, never applied.
    Mechanism details: the MCP server README.
    """
    with _driver(profile, confirm=allow_all, capability=Capability.HID) as (cfg, kvm):
        if not kvm.supports(Capability.VIDEO):
            raise ToolError(
                f"the '{cfg.driver}' driver has no video capture — calibration "
                "observes the cursor on snapshots, so it needs a capture device"
            )
        inv = act.new_invocation(
            host=cfg.host, profile=profile, tool="calibrate_mouse",
            effect=EffectClass.HID_INPUT, op="hid.mouse_calibrate", transport="hid.mouse",
            args={"tolerance": tolerance}, dry_run=_dry_run(),
        )
        approval = await act.approve_or_deny(ctx, inv, confirm=confirm)
        if not approval.approved:
            return {**_provenance(cfg), **act.result(inv, approval)}
        receipt, denied = _consume_receipt(inv, approval)
        if denied is not None:
            return {**_provenance(cfg), **denied}
        if inv.dry_run:
            # Pointer moves aren't in DESTRUCTIVE_OPS, so the driver's dry-run
            # guard would NOT intercept them — skip explicitly.
            return {
                **_provenance(cfg),
                **act.result(inv, approval, detail="dry-run: would calibrate", receipt=receipt),
            }
        try:
            cal = await anyio.to_thread.run_sync(
                lambda: run_calibration(kvm, host=cfg.host, tolerance=tolerance)
            )
            stored = save_calibration(cal)
        except CalibrationError as exc:
            act.audit_dispatch_error(inv, receipt, exc)
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            act.audit_dispatch_error(inv, receipt, exc)
            raise
        return {
            **_provenance(cfg),
            **act.result(
                inv, approval,
                detail=f"calibrated: residual {cal.residual:.3f} of screen "
                       f"(tolerance {tolerance}) at {cal.resolution}",
                extra={"calibration": cal.to_dict(), "stored": str(stored)},
                receipt=receipt,
            ),
        }


@mcp.tool(annotations=_READ)
def list_virtual_media(profile: str | None = None) -> dict:
    """Inventory the KVM's virtual-media (MSD) storage (read-only).

    Check this BEFORE asking the operator to download or upload an ISO — the
    image may already be on the device (#127). Returns stored images, the
    selected image, and attach state (``online``). ``host_visible_as`` (when
    known, #78) is the device name the TARGET's boot menu shows for truly
    presented media — match it to pick the right boot entry and to confirm the
    medium is really inserted. Details: `doctrine` topic 'interfaces'.
    """
    with _driver(profile, confirm=deny_all, capability=Capability.VIRTUAL_MEDIA) as (cfg, kvm):
        if not hasattr(kvm, "get_msd_state"):
            return {**_provenance(cfg), "note": "driver does not expose MSD storage inventory"}
        out = {**_provenance(cfg), "msd": kvm.get_msd_state()}
        pattern = getattr(kvm, "virtual_media_host_pattern", None)
        if pattern:
            out["host_visible_as"] = pattern
        return out


@mcp.tool(annotations=_REVERSIBLE_WRITE_REMOTE)
async def mount_iso(
    ctx: Context,
    source: str,
    name: str | None = None,
    usb: bool = False,
    confirm: bool = False,
    profile: str | None = None,
) -> dict:
    """Mount an ISO as virtual media on the host. GATED act (media effect gate); reversible.

    ``source`` is a local path or an ``http(s)://`` URL; ``usb=true`` attaches as a
    USB flash drive instead of a CD-ROM. Needs ``KVM_PILOT_MCP_ALLOW_MEDIA`` +
    per-invocation approval. Mounting bumps the frame generation, so a mouse click
    planned against the pre-mount screen is invalidated.
    """
    return await _act(
        ctx, profile, tool="mount_iso", effect=EffectClass.MEDIA, op="msd.connect",
        transport="msd", args={"source": source, "name": name, "usb": usb}, confirm=confirm,
        run=lambda _cfg, kvm: cast("VirtualMedia", kvm).mount_iso(source, image_name=name, cdrom=not usb),
        detail=f"mounted {source!r}",
        capability=Capability.VIRTUAL_MEDIA,
    )


@mcp.tool(annotations=_REVERSIBLE_WRITE)
async def eject(ctx: Context, confirm: bool = False, profile: str | None = None) -> dict:
    """Detach virtual media (the inverse of ``mount_iso``). GATED act (media effect gate); reversible.

    Needs ``KVM_PILOT_MCP_ALLOW_MEDIA`` + per-invocation approval.
    """
    return await _act(
        ctx, profile, tool="eject", effect=EffectClass.MEDIA, op="msd.disconnect",
        transport="msd", args={}, confirm=confirm,
        run=lambda _cfg, kvm: cast("VirtualMedia", kvm).msd_disconnect(),
        detail="ejected virtual media",
        capability=Capability.VIRTUAL_MEDIA,
    )


@mcp.tool(annotations=_READ)
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
async def ssh_exec(
    ctx: Context,
    command: str,
    confirm: bool = False,
    profile: str | None = None,
    host: str | None = None,
) -> dict:
    """Run a command on the managed host's OS over SSH. DESTRUCTIVE / in-band.

    Gated by its own SSH effect gate (never the HID gate) + per-invocation
    approval, with typed same-path denials (#234). ``host`` overrides the
    profile/env ``ssh_host`` at runtime (e.g. a discovered install-time DHCP
    address).
    """
    cfg = resolve_host(profile or os.environ.get("KVM_PILOT_PROFILE"))
    if host:
        cfg.ssh_host = host

    def _run() -> dict:
        # Channel built post-approval so a closed gate denies before any
        # config probing (the gate is the floor); a missing ssh_host then
        # surfaces as a clean tool error, audited as a dispatch exception.
        from kvm_pilot.ssh import SSHChannel

        try:
            ch = SSHChannel.from_config(cfg, confirm=allow_all, dry_run=_dry_run())
        except CapabilityError as exc:
            raise ToolError(str(exc)) from exc
        return ch.ssh_exec(command)

    return await _channel_act(
        ctx, profile, cfg, tool="ssh_exec", effect=EffectClass.SSH_EXEC,
        op="ssh.exec", transport="ssh.target",
        args={"command": command, "host": host}, confirm=confirm,
        run=_run, detail="ssh exec dispatched",
    )


@mcp.tool(annotations=_READ)
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
async def appliance_reboot(
    ctx: Context, confirm: bool = False, profile: str | None = None
) -> dict:
    """Reboot the KVM APPLIANCE (not the target) to clear a wedged encoder. DESTRUCTIVE.

    Recovers the RV1126 encoder wedge (the only fix — the stuck threads are
    unkillable kernel threads). Drops all KVM control for ~60s; the target's
    power is untouched. Gated by the appliance effect gate + per-invocation
    approval, with typed same-path denials (#234). There is no out-of-band
    power to the appliance, so use this deliberately, never in an automated loop.
    """
    cfg = resolve_host(profile or os.environ.get("KVM_PILOT_PROFILE"))

    def _run() -> dict:
        # Post-approval for the same reason as ssh_exec: gate first, config next.
        from kvm_pilot.ssh import ApplianceChannel

        try:
            ch = ApplianceChannel.from_config(cfg, confirm=allow_all, dry_run=_dry_run())
        except CapabilityError as exc:
            raise ToolError(str(exc)) from exc
        return ch.reboot()

    return await _channel_act(
        ctx, profile, cfg, tool="appliance_reboot", effect=EffectClass.APPLIANCE_RESET,
        op="appliance.reboot", transport="ssh.appliance", args={}, confirm=confirm,
        run=_run, detail="appliance reboot dispatched (KVM control drops ~60s)",
    )


@mcp.tool(annotations=_READ)
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


@mcp.tool(annotations=_REVERSIBLE_WRITE_REMOTE)
async def file_firmware_report(
    ctx: Context,
    confirm: bool = False,
    repo: str | None = None,
    source: str | None = None,
    dry_run: bool = False,
    profile: str | None = None,
) -> dict:
    """File the device's firmware-currency report as a GitHub issue when the
    registry is behind (MCP twin of CLI ``firmware-check``, #189/#190).
    EXTERNAL WRITE — writes outside the managed device.

    The read/reconcile half always runs; registry current → nothing to file,
    the result says so. Filing is gated as its own ``external_write`` effect
    (``KVM_PILOT_MCP_ALLOW_EXTERNAL_WRITE`` + per-invocation approval);
    ``dry_run=true`` previews the exact issue title/body; a missing or
    unauthenticated ``gh`` is a graceful ``filed=false`` reason.
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


@mcp.tool(annotations=_READ)
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
    _apply_read_only_mode()
    mcp.run()


if __name__ == "__main__":
    main()
