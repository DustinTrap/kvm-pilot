"""
Experimental MCP server for kvm-pilot.

Exposes a KVM device to MCP-capable agents (Claude Desktop, etc.) over stdio.
This is a SEPARATE component from the stdlib-only core library and depends on
the `mcp` SDK — install from ``mcp_server/requirements.txt``, not the core.

SAFETY MODEL (see mcp_server/README.md for the operator-facing version):
  * The read-only tools (``info``, ``healthcheck``, ``capabilities``,
    ``power_state``, ``logs``, ``snapshot``, ``classify_screen``) run with a
    deny-all confirm callback and carry a ``readOnlyHint`` tool annotation.
  * The one destructive tool (``power``) carries ``destructiveHint`` and is
    DISABLED until the human operator sets ``KVM_PILOT_MCP_ALLOW_POWER`` in the
    server's own environment. The model-supplied ``confirm`` flag is only a
    second factor, NOT human approval — MCP hosts should additionally require
    per-call human approval for this tool. Annotations are hints, never a
    security boundary.
  * ``KVM_PILOT_MCP_DRY_RUN=1`` builds every driver with ``dry_run=True``:
    destructive calls are logged and skipped, and results say so.
  * Drivers are built per call and closed afterwards — Redfish BMCs cap
    concurrent sessions device-side, so a leaked session locks operators out.

EXPERIMENTAL: the core library is an untested alpha (never run on real
hardware). Treat every result as unverified. See issue #7.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Literal

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from kvm_pilot import resolve_host
from kvm_pilot.config import HostConfig
from kvm_pilot.drivers import Capability, KVMDriver, make_driver_from_config
from kvm_pilot.safety import allow_all, deny_all
from kvm_pilot.vision import ScreenAnalyzer, VisionBackend, make_backend

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


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


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
        return {**_provenance(cfg), "info": kvm.get_info()}


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
        state = {**_provenance(cfg), "powered_on": kvm.is_powered_on()}
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
        return {**_provenance(cfg), "log": kvm.get_logs(seek=seek)}


@mcp.tool(annotations=_READ_ONLY)
def snapshot(profile: str | None = None):
    """Capture the current KVM screen and return it as a viewable JPEG image (read-only)."""
    with _driver(profile, confirm=deny_all, capability=Capability.VIDEO) as (cfg, kvm):
        note = f"screen of host '{cfg.host}' via the '{cfg.driver}' driver"
        if _dry_run():
            note += " (dry-run session)"
        return [note, Image(data=kvm.snapshot(), format="jpeg")]


@mcp.tool(annotations=_READ_ONLY)
def classify_screen(hint: str = "", profile: str | None = None) -> dict:
    """Classify the current screen's boot/run phase via the vision backend (read-only)."""
    with _driver(profile, confirm=deny_all, capability=Capability.VIDEO) as (cfg, kvm):
        state = ScreenAnalyzer(kvm, _vision_backend()).classify(hint=hint)
        return {**_provenance(cfg), **state.to_dict()}


@mcp.tool(annotations=_DESTRUCTIVE)
def power(
    action: Literal["on", "off", "off-hard", "reset"],
    confirm: bool = False,
    profile: str | None = None,
) -> str:
    """Change host power state. DESTRUCTIVE.

    Disabled unless the server operator has enabled power control in the server's
    own environment (see mcp_server/README.md). ``confirm=true`` is required as a
    second factor.
    """
    if not _env_flag("KVM_PILOT_MCP_ALLOW_POWER"):
        # Deliberately no copy-pasteable variable assignment here: the gate must be
        # opened by the human operator in the server's environment, out of band —
        # an agent must not be able to relay the exact incantation.
        raise ToolError(
            "power control is disabled on this server. Only the human operator can "
            "enable it, by setting the power-enable environment variable (documented "
            "in mcp_server/README.md) in the MCP server's own environment before "
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
