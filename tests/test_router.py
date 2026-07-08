"""Adaptive interface router (#181): selection across planes + staleness rules."""

from __future__ import annotations

from kvm_pilot.benchmark import CommandResult, Scorecard
from kvm_pilot.router import (
    INTERFACES,
    Interface,
    Plane,
    Trigger,
    cost_tier,
    is_stale,
    plane_of,
    select_interface,
    stale_rows,
)


def _card(*rows: CommandResult) -> Scorecard:
    return Scorecard(host="h", driver="glkvm", firmware=None, results=list(rows))


def test_registry_covers_every_interface_with_a_plane():
    assert set(INTERFACES) == set(Interface)
    assert plane_of(Interface.SSH) is Plane.OS
    assert plane_of(Interface.WINRM) is Plane.OS
    assert plane_of("library") is Plane.KVM
    assert cost_tier(Interface.LIBRARY) < cost_tier(Interface.CHROME)


def test_select_returns_only_capable_interface():
    card = _card(
        CommandResult("exec", "ssh", True, 276.0, 6),
        CommandResult("exec", "winrm", False, None, 0, "no powershell"),
    )
    assert select_interface(card, "exec").interface == "ssh"


def test_select_ranks_by_measured_latency_not_prior():
    # chrome has the worst cost tier but the cheapest *measured* p50 here -> it wins.
    card = _card(
        CommandResult("snapshot", "library", True, 150.0, 5),
        CommandResult("snapshot", "chrome", True, 50.0, 5),
    )
    assert select_interface(card, "snapshot").interface == "chrome"


def test_allow_planes_restricts_to_os_or_kvm():
    card = _card(
        CommandResult("run", "library", True, 10.0, 5),   # KVM plane, cheapest overall
        CommandResult("run", "ssh", True, 300.0, 5),        # OS plane
    )
    assert select_interface(card, "run").interface == "library"
    assert select_interface(card, "run", allow_planes={Plane.OS}).interface == "ssh"
    assert select_interface(card, "run", allow_planes={Plane.KVM}).interface == "library"


def test_no_capable_or_unknown_command_returns_none():
    card = _card(CommandResult("exec", "ssh", False, None, 0, "target down"))
    assert select_interface(card, "exec") is None
    assert select_interface(card, "does-not-exist") is None


def test_stale_rules_match_the_state_change_that_fired():
    card = _card(
        CommandResult("snapshot", "library", True, 150.0, 5),
        CommandResult("get_info", "library", True, 50.0, 5),
        CommandResult("exec", "ssh", True, 276.0, 6),
        CommandResult("ps_exec", "winrm", True, 300.0, 4),
    )

    def keys(trigger: Trigger) -> set[tuple[str, str]]:
        return {(r.command, r.interface) for r in stale_rows(card, trigger)}

    # a resolution change only invalidates the KVM-plane visual read
    assert keys(Trigger.RESOLUTION) == {("snapshot", "library")}
    # power off kills the screen reads AND every in-band OS interface
    powered = keys(Trigger.POWER)
    assert {("snapshot", "library"), ("exec", "ssh"), ("ps_exec", "winrm")} <= powered
    assert ("get_info", "library") not in powered
    # a firmware flash invalidates the whole KVM plane, nothing in-band
    fw = keys(Trigger.FIRMWARE)
    assert {("snapshot", "library"), ("get_info", "library")} <= fw
    assert ("exec", "ssh") not in fw
    # reachability changes scope to their own interface
    assert keys(Trigger.SSH_REACHABILITY) == {("exec", "ssh")}
    assert keys(Trigger.WINRM_REACHABILITY) == {("ps_exec", "winrm")}
    # reconnect / TTL invalidate everything
    assert len(stale_rows(card, Trigger.RECONNECT)) == 4
    assert len(stale_rows(card, Trigger.TTL)) == 4


def test_is_stale_is_the_row_level_predicate():
    ssh_row = CommandResult("exec", "ssh", True, 276.0, 6)
    assert is_stale(ssh_row, Trigger.POWER) is True          # OS plane dies with power
    assert is_stale(ssh_row, Trigger.RESOLUTION) is False     # resolution is a KVM concern
