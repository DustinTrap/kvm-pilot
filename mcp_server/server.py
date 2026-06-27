"""
Experimental MCP server for kvm-pilot.

Exposes a KVM device to MCP-capable agents (Claude Desktop, etc.) over stdio.
This is a SEPARATE component from the stdlib-only core library and depends on
the `mcp` SDK — install from ``mcp_server/requirements.txt``, not the core.

SAFETY: the read-only tools (``info``, ``power_state``, ``snapshot``,
``classify_screen``) run freely with a deny-all confirm callback. The one
destructive tool (``power``) REFUSES unless called with ``confirm=True`` — an
agent must opt in explicitly, so a model can never power-cycle a machine
implicitly. ``kvm_pilot.safety.SafetyPolicy`` remains the enforcement point.

EXPERIMENTAL: the core library is an untested alpha (never run on real
hardware). Treat every result as unverified. See issue #7.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from kvm_pilot import KVMClient, resolve_host
from kvm_pilot.safety import allow_all, deny_all
from kvm_pilot.vision import ScreenAnalyzer, make_backend

mcp = FastMCP("kvm-pilot")

_POWER_ACTIONS = {
    "on": "power_on",
    "off": "power_off",
    "off-hard": "power_off_hard",
    "reset": "reset_hard",
}


def _make_client(profile: str | None, *, confirm):
    cfg = resolve_host(profile or os.environ.get("KVM_PILOT_PROFILE"))
    return KVMClient.from_config(cfg, confirm=confirm)


@mcp.tool()
def info(profile: str | None = None) -> dict:
    """Return device / system info (read-only)."""
    return _make_client(profile, confirm=deny_all).get_info()


@mcp.tool()
def power_state(profile: str | None = None) -> dict:
    """Return ATX power state, including whether the host is powered on (read-only)."""
    kvm = _make_client(profile, confirm=deny_all)
    return {"powered_on": kvm.is_powered_on(), "atx": kvm.get_atx_state()}


@mcp.tool()
def snapshot(profile: str | None = None) -> str:
    """Capture the current KVM screen as a base64 JPEG (read-only)."""
    return _make_client(profile, confirm=deny_all).snapshot_base64()


@mcp.tool()
def classify_screen(hint: str = "", profile: str | None = None) -> dict:
    """Classify the current screen's boot/run phase via the vision backend (read-only)."""
    kvm = _make_client(profile, confirm=deny_all)
    analyzer = ScreenAnalyzer(kvm, make_backend("anthropic"))
    return analyzer.classify(hint=hint).to_dict()


@mcp.tool()
def power(action: str, confirm: bool = False, profile: str | None = None) -> str:
    """Power action: 'on', 'off', 'off-hard', or 'reset'.

    DESTRUCTIVE. Refuses unless ``confirm=True`` is passed explicitly.
    """
    if action not in _POWER_ACTIONS:
        return f"error: unknown action {action!r} (use on/off/off-hard/reset)"
    if not confirm:
        return (
            f"refused: '{action}' is destructive. Re-call with confirm=true to "
            f"power {action} the host."
        )
    kvm = _make_client(profile, confirm=allow_all)
    getattr(kvm, _POWER_ACTIONS[action])()
    return f"power {action}: requested"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
