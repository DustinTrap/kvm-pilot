"""Adaptive interface router (#181): pick the fastest *capable* interface, and
know when a prior measurement has gone stale.

Interfaces split across two **planes**:

* **KVM (out-of-band control)** — ``library`` / ``mcp`` / ``chrome``. Reach the
  machine at *any* state: BIOS, boot menu, a login screen, a hung OS. The only
  plane that can press a key at the firmware screen or read a blue-screen.
* **OS (in-band)** — ``ssh`` / ``winrm`` (remote PowerShell). Fast, structured
  text — but only when the target OS is up, on the network, and credentialed.
  For "get an inventory" this is one clean call vs. dozens of console frames.

The router scores a :class:`~kvm_pilot.benchmark.Scorecard` (which carries a row
per (command, interface)) and returns the cheapest capable row. Because
capability is **state-dependent** (a snapshot is JPEG or H.264 depending on
resolution/streamer; SSH works only while the OS is up), a scorecard is valid
only until state changes — :func:`stale_rows` says which rows a given change
invalidates so the caller re-benchmarks just those.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import StrEnum

from .benchmark import CommandResult, Scorecard


class Plane(StrEnum):
    KVM = "kvm"  # out-of-band control — works at any OS state
    OS = "os"    # in-band — needs the target OS up, reachable, credentialed


class Interface(StrEnum):
    LIBRARY = "library"
    MCP = "mcp"
    CHROME = "chrome"
    SSH = "ssh"
    WINRM = "winrm"  # remote PowerShell / PSRemoting (WS-Man or PowerShell-over-SSH)


@dataclass(frozen=True)
class InterfaceSpec:
    interface: Interface
    plane: Plane
    cost_tier: int  # coarse latency+cost prior (lower = cheaper); tiebreak / fallback
    summary: str


# Cost tiers come from the live 3-device sweep (#181): library ≈ MCP (~0.05-0.2s)
# < ssh/winrm (in-band exec, structured) ≪ chrome (seconds + a full-frame JPEG).
INTERFACES: dict[Interface, InterfaceSpec] = {
    Interface.LIBRARY: InterfaceSpec(Interface.LIBRARY, Plane.KVM, 1, "in-process driver call — the floor"),
    Interface.MCP: InterfaceSpec(Interface.MCP, Plane.KVM, 2, "MCP tools/call — persistent stdio, ~wire latency"),
    Interface.SSH: InterfaceSpec(Interface.SSH, Plane.OS, 3, "in-band shell on the target OS (structured text)"),
    Interface.WINRM: InterfaceSpec(Interface.WINRM, Plane.OS, 4, "remote PowerShell (WS-Man / PowerShell-over-SSH)"),
    Interface.CHROME: InterfaceSpec(Interface.CHROME, Plane.KVM, 9, "browser console — visual, seconds + heavy tokens"),
}


def plane_of(interface: Interface | str) -> Plane:
    return INTERFACES[Interface(interface)].plane


def cost_tier(interface: Interface | str) -> int:
    return INTERFACES[Interface(interface)].cost_tier


def select_interface(
    scorecard: Scorecard,
    command: str,
    *,
    allow_planes: set[Plane] | None = None,
) -> CommandResult | None:
    """The cheapest *capable* interface row for ``command`` — the router's core call.

    Ranks capable rows by measured warm p50 (falling back to the interface's cost
    tier when a row has no latency yet), tie-broken by cost tier. ``allow_planes``
    restricts the choice — e.g. ``{Plane.OS}`` for a host-shell intent, or
    ``{Plane.KVM}`` when the OS is down and only out-of-band control can act.
    Returns ``None`` when nothing can serve the command (the caller escalates —
    e.g. warm the streamer, lower resolution, or drop to the console).
    """
    rows = [
        r
        for r in scorecard.results
        if r.command == command
        and r.capable
        and (allow_planes is None or plane_of(r.interface) in allow_planes)
    ]
    if not rows:
        return None

    def rank(r: CommandResult) -> tuple[float, int]:
        latency = r.p50_ms if r.p50_ms is not None else cost_tier(r.interface) * 1000.0
        return (latency, cost_tier(r.interface))

    return min(rows, key=rank)


class Trigger(StrEnum):
    """A change in KVM/host state that can invalidate prior measurements."""

    FIRMWARE = "firmware"                       # a flash can change any KVM-side behavior
    RESOLUTION = "resolution"                   # snapshot codec (JPEG vs H.264) is res-gated
    STREAMER = "streamer"                        # on-demand encoder warm↔cold
    POWER = "power"                              # off kills the screen and the in-band OS
    RECONNECT = "reconnect"                      # a fresh connection — trust nothing
    SSH_REACHABILITY = "ssh_reachability"        # target came up / went away
    WINRM_REACHABILITY = "winrm_reachability"
    TTL = "ttl"                                  # age-based refresh


def is_stale(row: CommandResult, trigger: Trigger) -> bool:
    """Whether ``row`` must be re-benchmarked after ``trigger`` fired.

    This is the "re-assess as state changes" rule, made concrete: a resolution
    change only invalidates the KVM-plane ``snapshot`` rows, a power-off
    invalidates the screen reads *and* every in-band OS row, a firmware flash
    invalidates the whole KVM plane, and a reconnect/TTL invalidates everything.
    """
    iface = Interface(row.interface)
    plane = plane_of(iface)
    if trigger in (Trigger.RECONNECT, Trigger.TTL):
        return True
    if trigger is Trigger.FIRMWARE:
        return plane is Plane.KVM
    if trigger in (Trigger.RESOLUTION, Trigger.STREAMER):
        return plane is Plane.KVM and row.command == "snapshot"
    if trigger is Trigger.POWER:
        return plane is Plane.OS or row.command in ("snapshot", "boot_progress")
    if trigger is Trigger.SSH_REACHABILITY:
        return iface is Interface.SSH
    if trigger is Trigger.WINRM_REACHABILITY:
        return iface is Interface.WINRM
    return False


def stale_rows(scorecard: Scorecard, trigger: Trigger) -> list[CommandResult]:
    """The scorecard rows a ``trigger`` invalidates (re-benchmark exactly these)."""
    return [r for r in scorecard.results if is_stale(r, trigger)]


# -- persistence: a remembered per-device profile --------------------------
#
# So the router doesn't re-benchmark on every call, a scorecard is cached to
# disk per host and reloaded — with a firmware check that drops the KVM-plane
# rows if the device was reflashed (they're the ones a flash can change; #180).

_SCORECARD_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config")),
    "kvm-pilot", "scorecards",
)


def scorecard_path(host: str, *, base: str | None = None) -> str:
    safe = host.replace("/", "_").replace(":", "_")
    return os.path.join(base or _SCORECARD_DIR, f"{safe}.json")


def save_scorecard(card: Scorecard, path: str | None = None) -> str:
    path = path or scorecard_path(card.host)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(card.to_dict(), fh, indent=2)
    return path


def load_scorecard(path: str) -> Scorecard:
    with open(path, encoding="utf-8") as fh:
        return Scorecard.from_dict(json.load(fh))


def load_for(
    host: str, *, firmware: str | None = None, path: str | None = None
) -> Scorecard | None:
    """The cached scorecard for ``host``, or ``None`` if there's no cache.

    If ``firmware`` differs from when it was saved, the KVM-plane rows are
    dropped (a flash can change any of them, #180) so the caller re-benchmarks
    just those; the in-band OS-plane rows and the record are kept.
    """
    path = path or scorecard_path(host)
    if not os.path.exists(path):
        return None
    card = load_scorecard(path)
    if firmware is not None and card.firmware != firmware:
        card.results = [r for r in card.results if not is_stale(r, Trigger.FIRMWARE)]
        card.firmware = firmware
    return card
