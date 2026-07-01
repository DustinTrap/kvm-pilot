"""
Experimental MCP server for kvm-pilot.

Exposes a KVM device to MCP-capable agents (Claude Desktop, etc.) over stdio.
This is a SEPARATE component from the stdlib-only core library and depends on
the `mcp` SDK — install from ``mcp_server/requirements.txt``, not the core.

SAFETY MODEL (see mcp_server/README.md for the operator-facing version):
  * The read-only tools (``info``, ``power_state``, ``snapshot``,
    ``classify_screen``) run with a deny-all confirm callback and carry a
    ``readOnlyHint`` tool annotation.
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
    profile: str | None, *, confirm: Callable[[str, str], bool], capability: Capability
) -> Iterator[tuple[HostConfig, KVMDriver]]:
    """Resolve the profile, build its driver, and always close it afterwards.

    The capability gate is checked structurally (``supports()``, no network) so a
    tool the driver cannot serve fails with a clear MCP tool error instead of an
    ``AttributeError`` — e.g. a Redfish BMC has no video capture.
    """
    cfg = resolve_host(profile or os.environ.get("KVM_PILOT_PROFILE"))
    kvm = make_driver_from_config(cfg, confirm=confirm, dry_run=_dry_run())
    try:
        if not kvm.supports(capability):
            raise ToolError(
                f"the '{cfg.driver}' driver for host '{cfg.host}' does not provide the "
                f"'{capability.value}' capability this tool needs"
            )
        yield cfg, kvm
    finally:
        close = getattr(kvm, "close", None)
        if close is not None:
            close()


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
    with _driver(profile, confirm=allow_all, capability=Capability.POWER) as (cfg, kvm):
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
