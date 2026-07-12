"""Unit tests for the MCP act-layer authorization (``kvm_pilot.mcp.act``, #61).

In-process (no subprocess): the effect gate, the fail-closed profile allowlist,
the invocation/receipt shape, and the two-guarantee approval in its
pre-authorized posture (``ctx=None`` -> no elicitation -> confirm required).
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

from kvm_pilot.mcp import act  # noqa: E402
from kvm_pilot.safety import EffectClass  # noqa: E402

_ENV = (
    "KVM_PILOT_MCP_ALLOW_HID",
    "KVM_PILOT_MCP_ALLOW_POWER",
    "KVM_PILOT_MCP_ALLOW_MEDIA",
    "KVM_PILOT_MCP_PROFILES",
    "KVM_PILOT_PROFILE",
    "KVM_PILOT_HOST",
    "KVM_PILOT_MCP_DRY_RUN",
    "KVM_PILOT_MCP_ELICIT",
)


def _clear(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)


# -- effect gate -------------------------------------------------------------


def test_gate_observe_always_open(monkeypatch):
    _clear(monkeypatch)
    assert act.gate_enabled(EffectClass.OBSERVE) is True


def test_gate_hid_needs_allow_hid(monkeypatch):
    _clear(monkeypatch)
    assert act.gate_enabled(EffectClass.HID_INPUT) is False
    monkeypatch.setenv("KVM_PILOT_MCP_ALLOW_HID", "1")
    assert act.gate_enabled(EffectClass.HID_INPUT) is True


def test_gate_power_soft_needs_power_not_hid(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("KVM_PILOT_MCP_ALLOW_HID", "1")
    assert act.gate_enabled(EffectClass.POWER_SOFT) is False  # HID gate is insufficient
    monkeypatch.setenv("KVM_PILOT_MCP_ALLOW_POWER", "1")
    assert act.gate_enabled(EffectClass.POWER_SOFT) is True


# -- fail-closed profile allowlist -------------------------------------------


def test_allowlist_absent_is_passthrough(monkeypatch):
    _clear(monkeypatch)
    assert act.enforce_allowlist("anything") == "anything"


def test_allowlist_present_but_empty_refuses_all(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("KVM_PILOT_MCP_PROFILES", "")  # set-but-empty -> lockdown
    with pytest.raises(ToolError):
        act.enforce_allowlist("fakebox")


def test_allowlist_refuses_unlisted_profile(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("KVM_PILOT_MCP_PROFILES", "fakebox, prod")
    assert act.enforce_allowlist("fakebox") == "fakebox"  # trimmed, listed
    with pytest.raises(ToolError, match="allowlist"):
        act.enforce_allowlist("bmc")


def test_allowlist_env_pinned_host_allowed_without_profile(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("KVM_PILOT_MCP_PROFILES", "fakebox")
    monkeypatch.setenv("KVM_PILOT_HOST", "10.0.0.1")
    assert act.enforce_allowlist(None) is None  # env-pinned single host, can't roam


def test_allowlist_ambiguous_default_refused(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("KVM_PILOT_MCP_PROFILES", "fakebox")
    with pytest.raises(ToolError):
        act.enforce_allowlist(None)  # no profile and no pinned host -> ambiguous


# -- invocation / receipt shape ----------------------------------------------


def _inv(**kw):
    base = dict(
        host="h", profile="p", tool="type_text", effect=EffectClass.HID_INPUT,
        op="hid.type_text", transport="hid.keyboard", args={"text": "x"}, dry_run=False,
    )
    base.update(kw)
    return act.new_invocation(**base)


def test_args_hash_is_stable_and_order_independent():
    a = _inv(args={"a": 1, "b": 2})
    b = _inv(args={"b": 2, "a": 1})
    assert a.args_hash == b.args_hash
    assert a.invocation_id != b.invocation_id  # unique per invocation


def test_result_records_transport_and_effect():
    inv = _inv()
    out = act.result(inv, act.Approval(True, approver="operator"), detail="typed 1")
    assert out["effect"] == "hid_input"
    assert out["transport"] == "hid.keyboard"  # anti-bypass: transport AND effect
    assert out["op"] == "hid.type_text"
    assert out["approved"] is True
    assert out["invocation_id"] == inv.invocation_id
    assert out["approval"]["args_hash"] == inv.args_hash
    assert out["approval"]["expires"] is None  # no receipt issued for this result (#72)


# -- approve_or_deny (pre-authorized posture: ctx=None -> no elicitation) -----


def test_approval_denied_when_gate_closed(monkeypatch):
    _clear(monkeypatch)
    res = asyncio.run(act.approve_or_deny(None, _inv(), confirm=True))
    assert res.approved is False
    assert "disabled" in res.reason


def test_approval_preauthorized_requires_confirm(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("KVM_PILOT_MCP_ALLOW_HID", "1")
    assert asyncio.run(act.approve_or_deny(None, _inv(), confirm=False)).approved is False
    ok = asyncio.run(act.approve_or_deny(None, _inv(), confirm=True))
    assert ok.approved is True
    assert ok.approver == "policy"


def test_approval_invalidated_on_midflight_state_change(monkeypatch):
    # If dry-run / the gate flips while the approval is pending, invalidate it.
    _clear(monkeypatch)
    monkeypatch.setenv("KVM_PILOT_MCP_ALLOW_HID", "1")
    states = iter([(False, True), (True, True)])  # snapshot, then changed
    monkeypatch.setattr(act, "_bound_state", lambda effect: next(states))
    res = asyncio.run(act.approve_or_deny(None, _inv(), confirm=True))
    assert res.approved is False
    assert "invalidated" in res.reason


# -- generation-keyed frame identity (#124) ----------------------------------


def test_frame_ref_format_and_generation_parse():
    ref = act.frame_ref("10.0.0.5", b"abc")
    host, gen, digest = ref.rsplit(":", 2)
    assert host == "10.0.0.5"
    assert gen == "0"
    assert len(digest) == 16
    assert act.frame_ref_generation(ref) == 0


def test_frame_ref_generation_handles_colon_bearing_host():
    # rsplit(':', 2) keeps an IPv6 / host:port intact.
    assert act.frame_ref_generation("fe80::1:3:deadbeefdeadbeef") == 3


def test_frame_ref_generation_none_on_malformed():
    assert act.frame_ref_generation("garbage") is None
    assert act.frame_ref_generation("h:notanint:hash") is None


def test_bump_generation_increments_per_host():
    h = "gen-increment-host"
    start = act.generation(h)
    assert act.bump_generation(h) == start + 1
    assert act.generation(h) == start + 1


def test_bumps_generation_only_for_media_and_power():
    assert act.bumps_generation(EffectClass.MEDIA) is True
    assert act.bumps_generation(EffectClass.POWER_SOFT) is True
    assert act.bumps_generation(EffectClass.POWER_HARD) is True
    assert act.bumps_generation(EffectClass.HID_INPUT) is False
    assert act.bumps_generation(EffectClass.HID_CONTROL) is False


@pytest.mark.parametrize(
    "p,expected",
    [(0.0, -32768), (0.5, 0), (1.0, 32767), (-1.0, -32768), (2.0, 32767)],
)
def test_pct_to_kvmd_maps_and_clamps(p, expected):
    assert act.pct_to_kvmd(p) == expected


# -- external-write gate + fail-closed contract (#190) ----------------------- #


def test_every_effect_class_has_a_registered_gate():
    # The fail-closed contract: gate_enabled() refuses any effect missing from
    # EFFECT_ENABLE_FLAG, so every member must be deliberately registered —
    # a new effect class must get its own flag, never borrow another gate.
    missing = set(EffectClass) - set(act.EFFECT_ENABLE_FLAG)
    assert not missing, f"EffectClass members without a gate mapping: {sorted(missing)}"


def test_gate_fails_closed_for_unmapped_effect(monkeypatch):
    monkeypatch.setenv("KVM_PILOT_MCP_ALLOW_CONFIG", "1")  # must NOT be borrowed
    monkeypatch.delitem(act.EFFECT_ENABLE_FLAG, EffectClass.EXTERNAL_WRITE)
    assert act.gate_enabled(EffectClass.EXTERNAL_WRITE) is False


def test_gate_external_write_needs_its_own_flag(monkeypatch):
    for flag in ("KVM_PILOT_MCP_ALLOW_CONFIG", "KVM_PILOT_MCP_ALLOW_SSH",
                 "KVM_PILOT_MCP_ALLOW_POWER"):
        monkeypatch.setenv(flag, "1")  # other gates must not open this one
    assert act.gate_enabled(EffectClass.EXTERNAL_WRITE) is False
    monkeypatch.setenv("KVM_PILOT_MCP_ALLOW_EXTERNAL_WRITE", "1")
    assert act.gate_enabled(EffectClass.EXTERNAL_WRITE) is True


def test_client_kill_counter_resets_on_approval(monkeypatch):
    # #149: the ELICIT hint keys off CONSECUTIVE kills — an approval in between
    # resets the streak, so the next one-off cancel stays hint-free.
    act._clear_client_kills("h1")
    assert act._note_client_kill("h1") == 1
    assert act._note_client_kill("h1") == 2
    act._clear_client_kills("h1")
    assert act._note_client_kill("h1") == 1
    act._clear_client_kills("h1")


def test_kill_hint_only_at_threshold():
    act._clear_client_kills("h2")
    inv = _inv(host="h2")
    first = act._with_kill_hint("base remedy.", inv)
    assert "KVM_PILOT_MCP_ELICIT=off" not in first
    second = act._with_kill_hint("base remedy.", inv)
    assert "KVM_PILOT_MCP_ELICIT=off" in second and "#2 in a row" in second
    act._clear_client_kills("h2")


# -- #72: signed, expiring, single-use approval receipts --------------------- #
#
# The negative paths are the point (per the issue's acceptance shape): every
# destructive act attempt either has a fresh matching approval receipt, or a
# machine-readable reason it did not execute.


def _approved_receipt(monkeypatch, **inv_kw):
    _clear(monkeypatch)
    monkeypatch.setenv("KVM_PILOT_MCP_ALLOW_HID", "1")
    inv = _inv(**inv_kw)
    approval = asyncio.run(act.approve_or_deny(None, inv, confirm=True))
    assert approval.approved is True
    return inv, approval, act.issue_receipt(inv, approval)


def test_receipt_happy_path_consumes_once(monkeypatch):
    inv, approval, receipt = _approved_receipt(monkeypatch)
    assert act.receipt_state(receipt.receipt_id) == "issued"
    assert act.verify_and_consume(receipt, inv) is None
    assert act.receipt_state(receipt.receipt_id) == "consumed"
    out = act.result(inv, approval, receipt=receipt)
    assert out["receipt"]["id"] == receipt.receipt_id
    assert out["receipt"]["state"] == "consumed"
    assert out["approval"]["expires"] is not None    # a real ISO expiry now (#72)


def test_receipt_replay_is_refused(monkeypatch):
    inv, _approval, receipt = _approved_receipt(monkeypatch)
    assert act.verify_and_consume(receipt, inv) is None
    denial = act.verify_and_consume(receipt, inv)    # same human decision, reused
    assert denial is not None and "already consumed" in denial
    assert act.receipt_state(receipt.receipt_id) == "consumed"  # state unchanged


def test_receipt_refused_when_args_change_after_approval(monkeypatch):
    inv, _approval, receipt = _approved_receipt(monkeypatch)
    mutated = act.new_invocation(
        host=inv.host, profile=inv.profile, tool=inv.tool, effect=inv.effect,
        op=inv.op, transport=inv.transport, args={"text": "rm -rf /"}, dry_run=inv.dry_run,
    )
    # Same receipt presented for a different invocation: mismatched, not consumed.
    denial = act.verify_and_consume(receipt, mutated)
    assert denial is not None and "mismatched" in denial
    assert act.receipt_state(receipt.receipt_id) == "issued"


def test_receipt_refused_when_dry_run_flips_to_live(monkeypatch):
    import dataclasses

    inv, _approval, receipt = _approved_receipt(monkeypatch, dry_run=True)
    live = dataclasses.replace(inv, dry_run=False)   # approval was for a dry run
    denial = act.verify_and_consume(receipt, live)
    assert denial is not None and "mismatched" in denial


def test_receipt_refused_when_host_changes(monkeypatch):
    import dataclasses

    inv, _approval, receipt = _approved_receipt(monkeypatch)
    denial = act.verify_and_consume(receipt, dataclasses.replace(inv, host="evil-host"))
    assert denial is not None and "mismatched" in denial


def test_receipt_tampered_mac_is_refused(monkeypatch):
    import dataclasses

    inv, _approval, receipt = _approved_receipt(monkeypatch)
    forged = dataclasses.replace(receipt, mac="0" * 64)
    denial = act.verify_and_consume(forged, inv)
    assert denial is not None and "mismatched" in denial


def test_receipt_expires(monkeypatch):
    inv, _approval, receipt = _approved_receipt(monkeypatch)
    denial = act.verify_and_consume(receipt, inv, now=receipt.expires_at + 1)
    assert denial is not None and "expired" in denial
    assert act.receipt_state(receipt.receipt_id) == "expired"


def test_receipt_ttl_env_is_honored_and_clamped(monkeypatch):
    monkeypatch.setenv("KVM_PILOT_MCP_RECEIPT_TTL", "120")
    assert act._receipt_ttl() == 120.0
    monkeypatch.setenv("KVM_PILOT_MCP_RECEIPT_TTL", "0")     # fail-safe: never zero
    assert act._receipt_ttl() == 1.0
    monkeypatch.setenv("KVM_PILOT_MCP_RECEIPT_TTL", "junk")  # fail-safe: default
    assert act._receipt_ttl() == 60.0


def test_receipt_store_growth_is_bounded(monkeypatch):
    inv, approval, _receipt = _approved_receipt(monkeypatch)
    for _ in range(act._RECEIPT_MAX_TRACKED + 10):
        act.issue_receipt(inv, approval)
    assert len(act._RECEIPTS) <= act._RECEIPT_MAX_TRACKED + 1


def test_audit_records_every_terminal(monkeypatch, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="kvm_pilot.mcp.audit"):
        inv, _approval, receipt = _approved_receipt(monkeypatch)
        act.verify_and_consume(receipt, inv)                  # consumed
        act.verify_and_consume(receipt, inv)                  # replayed
        act.audit_dispatch_error(inv, receipt, RuntimeError("boom"))
    events = [json.loads(r.message)["event"] for r in caplog.records]
    assert "approved" in events and "issued" in events
    assert "consumed" in events and "replayed" in events
    assert "dispatch-exception" in events
    # Every record carries the invocation id — the audit trail's join key.
    assert all(json.loads(r.message)["invocation_id"] == inv.invocation_id
               for r in caplog.records if json.loads(r.message).get("event") != "denied")
