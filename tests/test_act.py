"""Unit tests for the MCP act-layer authorization (``kvm_pilot.mcp.act``, #61).

In-process (no subprocess): the effect gate, the fail-closed profile allowlist,
the invocation/receipt shape, and the two-guarantee approval in its
pre-authorized posture (``ctx=None`` -> no elicitation -> confirm required).
"""

from __future__ import annotations

import asyncio

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
    assert out["approval"]["expires"] is None  # signed/expiring receipt deferred to #72


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
