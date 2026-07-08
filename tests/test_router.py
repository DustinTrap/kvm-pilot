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


# -- online learning: Scorecard.record (#181 increment 4) -------------------

def test_record_nudges_p50_via_ewma_and_counts_the_sample():
    card = _card(CommandResult("get_info", "library", True, 100.0, 5))
    card.record("get_info", "library", 200.0, ok=True)
    r = card.results[0]
    assert r.samples == 6
    assert r.capable is True
    assert r.p50_ms == 130.0  # EWMA(100, 200, alpha=0.3) = 0.7*100 + 0.3*200


def test_record_failure_marks_row_incapable():
    card = _card(CommandResult("exec", "ssh", True, 50.0, 3))
    card.record("exec", "ssh", None, ok=False)
    assert card.results[0].capable is False   # router stops picking it until it succeeds again
    assert "failed" in card.results[0].note


def test_record_appends_an_unknown_command_interface_pair():
    card = _card(CommandResult("exec", "ssh", True, 50.0, 3))
    card.record("ps_exec", "winrm", 300.0, ok=True)
    added = next(r for r in card.results if r.command == "ps_exec")
    assert added.interface == "winrm" and added.capable and added.p50_ms == 300.0


# -- persistence + firmware-aware cache load (#181 increment 3) --------------

def test_scorecard_dict_round_trip():
    card = Scorecard(host="h", driver="glkvm", firmware="V1.9.1", results=[
        CommandResult("snapshot", "library", True, 150.0, 5, "note"),
        CommandResult("exec", "ssh", False, None, 0, "down"),
    ])
    back = Scorecard.from_dict(card.to_dict())
    assert back.host == "h" and back.firmware == "V1.9.1"
    assert [(r.command, r.interface, r.capable, r.p50_ms) for r in back.results] == \
           [(r.command, r.interface, r.capable, r.p50_ms) for r in card.results]


def test_save_load_and_path_sanitizing(tmp_path):
    from kvm_pilot.router import load_scorecard, save_scorecard, scorecard_path

    assert scorecard_path("10.0.1.39", base="/tmp/x").endswith("10.0.1.39.json")
    card = _card(CommandResult("get_info", "library", True, 100.0, 5))
    path = str(tmp_path / "sc.json")
    save_scorecard(card, path)
    assert load_scorecard(path).results[0].p50_ms == 100.0


def test_load_for_missing_cache_is_none(tmp_path):
    from kvm_pilot.router import load_for
    assert load_for("nope", path=str(tmp_path / "missing.json")) is None


def test_load_for_firmware_change_drops_kvm_rows_keeps_os(tmp_path):
    from kvm_pilot.router import load_for, save_scorecard

    path = str(tmp_path / "h.json")
    save_scorecard(Scorecard(host="h", driver="glkvm", firmware="V1.5.1", results=[
        CommandResult("snapshot", "library", True, 150.0, 5),  # KVM plane
        CommandResult("get_info", "library", True, 50.0, 5),    # KVM plane
        CommandResult("exec", "ssh", True, 60.0, 5),             # OS plane
    ]), path)

    same = load_for("h", firmware="V1.5.1", path=path)
    assert len(same.results) == 3                                # unchanged firmware → all kept

    changed = load_for("h", firmware="V1.9.1", path=path)         # a flash invalidates the KVM plane
    assert {(r.command, r.interface) for r in changed.results} == {("exec", "ssh")}
    assert changed.firmware == "V1.9.1"
