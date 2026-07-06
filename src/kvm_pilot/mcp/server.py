"""
Experimental MCP server for kvm-pilot.

Exposes a KVM device to MCP-capable agents (Claude Desktop, etc.) over stdio.
It ships in the wheel (``pip install kvm-pilot``) and is launched via the
``kvm-pilot-mcp`` console script or ``python -m kvm_pilot.mcp.server``. It depends
on the `mcp` SDK, which is a base dependency (the client/driver code itself stays
stdlib-only, importing ``mcp`` only here).

SAFETY MODEL (see the co-located README.md for the operator-facing version):
  * The read-only tools (``info``, ``healthcheck``, ``capabilities``,
    ``power_state``, ``logs``, ``snapshot``, ``classify_screen``) run with a
    deny-all confirm callback and carry a ``readOnlyHint`` tool annotation.
  * The destructive tools (``power`` and the HID act tools ``type_text`` /
    ``press_key`` / ``send_shortcut`` / ``ctrl_alt_delete``) carry
    ``destructiveHint`` and are DISABLED until the operator opts the tool's
    *effect class* in via an env flag in the server's own environment
    (``KVM_PILOT_MCP_ALLOW_POWER`` / ``KVM_PILOT_MCP_ALLOW_HID``). On top of that
    each act call requires per-invocation approval — a human MCP elicitation when
    the client supports it, else an explicit ``confirm=true`` under the operator's
    standing policy. Tools are classified by effect not transport, so a reboot
    (``ctrl_alt_delete``, a Ctrl+Alt+Del chord) needs the power gate, not the HID
    gate. See ``act.py`` and the co-located README. Annotations are hints, never a
    security boundary.
  * ``KVM_PILOT_MCP_DRY_RUN=1`` builds every driver with ``dry_run=True``:
    destructive calls are logged and skipped, and results say so.
  * Drivers are built per call and closed afterwards — Redfish BMCs cap
    concurrent sessions device-side, so a leaked session locks operators out.

EXPERIMENTAL: the core library is an untested alpha (never run on real
hardware). Treat every result as unverified. See issue #7.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal, cast

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from kvm_pilot import resolve_host
from kvm_pilot.config import HostConfig
from kvm_pilot.drivers import Capability, KVMDriver, make_driver_from_config
from kvm_pilot.errors import CapabilityError, VisionError
from kvm_pilot.mcp import act
from kvm_pilot.safety import EffectClass, allow_all, deny_all, shortcut_effect
from kvm_pilot.vision import (
    ALL_PHASES,
    SYSTEM_PROMPT,
    ScreenAnalyzer,
    VisionBackend,
    make_backend,
)

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
    the capability enum's declaration order for stable output.
    """
    with _driver(profile, confirm=deny_all, preflight=False) as (cfg, kvm):
        caps = kvm.capabilities()
        return {**_provenance(cfg), "capabilities": [c.value for c in Capability if c in caps]}


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
    """
    with _driver(profile, confirm=deny_all, capability=Capability.VIDEO) as (cfg, kvm):
        img = cast("Video", kvm).snapshot()
        note = f"screen of host '{cfg.host}' via the '{cfg.driver}' driver"
        if _dry_run():
            note += " (dry-run session)"
        payload = {**_provenance(cfg), "frame_ref": act.frame_ref(cfg.host, img), "note": note}
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


@mcp.tool(annotations=_DESTRUCTIVE)
def power(
    action: Literal["on", "off", "off-hard", "reset"],
    confirm: bool = False,
    profile: str | None = None,
) -> str:
    """Change host power state. DESTRUCTIVE.

    Disabled unless the server operator has enabled power control in the server's
    own environment (see the co-located README.md). ``confirm=true`` is required as
    a second factor.
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
            return (
                f"power {action}: DRY-RUN — logged only, nothing was sent to "
                f"host '{cfg.host}' ({cfg.driver})"
            )
        return f"power {action}: requested on host '{cfg.host}' ({cfg.driver})"


# -- HID act tools (issue #61): see act.py for the two-guarantee model --------


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
        run(kvm)
        # A media/power effect changes the screen enough to invalidate prior
        # observations — bump the frame generation so a stale mouse ref won't match.
        if not inv.dry_run and act.bumps_generation(inv.effect):
            act.bump_generation(cfg.host)
        return {**_provenance(cfg), **act.result(inv, approval, detail=detail)}


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


def _mouse_stale(host: str, observed_frame_ref: str | None) -> str | None:
    """A denial reason if the observation is missing or from a stale generation, else None."""
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
    the ``observed_frame_ref`` it was planned against (from a prior ``snapshot``). If
    the host has since rebooted or swapped media (its frame *generation* changed) the
    call is refused so the click can't land on a stale screen — re-``snapshot`` and
    retry. A move-only call (``button`` omitted) needs no ref.

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
        if not inv.dry_run:
            _run_mouse(kvm, x, y, coord_space, button)
        detail = f"moved to ({x}, {y}) [{coord_space}]" + (f" + {button} click" if button else "")
        return {
            **_provenance(cfg),
            **act.result(inv, approval, detail=detail, extra={"coord_space": coord_space}),
        }


@mcp.tool(annotations=_READ_ONLY)
def list_virtual_media(profile: str | None = None) -> dict:
    """Inventory the KVM's virtual-media (MSD) storage (read-only).

    Check this BEFORE asking the operator to download or upload an ISO — the
    image you need may already be on the device from an earlier job (#127).
    Returns the device's MSD state: stored images (name/size/completeness),
    the selected drive image, and whether media is attached (``online``).
    """
    with _driver(profile, confirm=deny_all, capability=Capability.VIRTUAL_MEDIA) as (cfg, kvm):
        if not hasattr(kvm, "get_msd_state"):
            return {**_provenance(cfg), "note": "driver does not expose MSD storage inventory"}
        return {**_provenance(cfg), "msd": kvm.get_msd_state()}


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
