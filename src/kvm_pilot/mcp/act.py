"""Per-invocation authorization for the MCP act tools (issue #61).

Two guarantees per act call, from the Armorer Labs security review:

  (a) **allowed** — the effect class is operator-enabled (an env flag set in the
      server's own environment) *and* the target profile is on the allowlist;
  (b) **approved at run time** — either a human approved this exact invocation via
      MCP elicitation (*interactive* posture), or a standing operator policy
      pre-authorized it and the caller passed ``confirm=True`` (*pre-authorized /
      unattended* posture — required so the headline unattended-install loop still
      works when no human is present to answer an elicitation).

Denials/expiries (gate closed, declined, cancelled, not confirmed, context
changed mid-approval) return through the SAME tool call path — a result dict with
``approved=False`` and a reason — so an agent can recover, never an out-of-band
hang. The result records both ``transport`` and ``effect`` so an actuator can't
launder a power effect by choosing a different tool.

The full signed/expiring consent receipt is deferred to #72; the stable
``invocation_id`` + effect class are already in the result shape here so #72 can
build on them.

This module lives under ``mcp/`` (not core), so it may import ``mcp`` and
``pydantic``; it imports nothing from ``server`` to keep the dependency
one-directional (server -> act).
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ClientCapabilities, ElicitationCapability
from pydantic import BaseModel

from kvm_pilot.safety import EffectClass

# --------------------------------------------------------------------------- #
# Guarantee (a): the effect class is operator-enabled                         #
# --------------------------------------------------------------------------- #

# Each effect maps to the operator env flag that enables it (None = never gated).
# HID input and HID control share the HID flag; both power tiers share the power
# flag (so Ctrl+Alt+Del, classified power_soft, needs ALLOW_POWER — it cannot be
# reached via the weaker HID gate).
EFFECT_ENABLE_FLAG: dict[EffectClass, str | None] = {
    EffectClass.OBSERVE: None,
    EffectClass.HID_INPUT: "KVM_PILOT_MCP_ALLOW_HID",
    EffectClass.HID_CONTROL: "KVM_PILOT_MCP_ALLOW_HID",
    EffectClass.MEDIA: "KVM_PILOT_MCP_ALLOW_MEDIA",
    EffectClass.POWER_SOFT: "KVM_PILOT_MCP_ALLOW_POWER",
    EffectClass.POWER_HARD: "KVM_PILOT_MCP_ALLOW_POWER",
    EffectClass.CONFIG_MUTATION: "KVM_PILOT_MCP_ALLOW_CONFIG",
}


def env_flag(name: str) -> bool:
    """True if env var ``name`` is set to a truthy value (1/true/yes)."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def gate_enabled(effect: EffectClass) -> bool:
    """True if the operator enabled this effect class (or it needs no gate)."""
    flag = EFFECT_ENABLE_FLAG.get(effect, "KVM_PILOT_MCP_ALLOW_CONFIG")
    return flag is None or env_flag(flag)


def enforce_allowlist(profile: str | None) -> str | None:
    """Fail-closed profile allowlist (``KVM_PILOT_MCP_PROFILES``). Returns the
    effective profile name, or raises ``ToolError`` if the target isn't allowed.

    - Var **absent** -> no allowlist (today's behavior, back-compat).
    - Var **present but empty** -> allow nothing (an operator who sets it to ``''``
      by intent or slip gets lockdown, not wide-open).
    - A named profile not in the list -> refused; configured targets are never a
      silent fall-back.
    - No named profile *and* a single host pinned via ``KVM_PILOT_HOST`` -> allowed
      (env-pinned, can't roam). No profile and no pinned host -> refused (ambiguous).
    """
    if "KVM_PILOT_MCP_PROFILES" not in os.environ:
        return profile
    allowed = {p.strip() for p in os.environ["KVM_PILOT_MCP_PROFILES"].split(",") if p.strip()}
    effective = profile or os.environ.get("KVM_PILOT_PROFILE")
    if effective is None:
        if os.environ.get("KVM_PILOT_HOST"):
            return None  # env-pinned single host; the allowlist governs profile names
        raise ToolError(
            "this server has a profile allowlist (KVM_PILOT_MCP_PROFILES) but no profile "
            "was selected and no single host is pinned — refusing an ambiguous target"
        )
    if effective not in allowed:
        raise ToolError(
            f"profile {effective!r} is not in this server's allowlist "
            "(KVM_PILOT_MCP_PROFILES); refusing (configured targets are not a fallback)"
        )
    return effective


# --------------------------------------------------------------------------- #
# Guarantee (b): approved at run time                                         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class InvocationContext:
    """The invocation an approval binds to (Armorer's binding tuple).

    The signed/expiring receipt is deferred to #72; ``invocation_id`` + ``effect``
    are stable here so that layer can build on them.
    """

    invocation_id: str
    host: str
    profile: str | None
    tool: str
    effect: EffectClass
    op: str
    transport: str
    args_hash: str
    dry_run: bool


def new_invocation(
    *,
    host: str,
    profile: str | None,
    tool: str,
    effect: EffectClass,
    op: str,
    transport: str,
    args: dict[str, Any],
    dry_run: bool,
) -> InvocationContext:
    return InvocationContext(
        invocation_id=uuid.uuid4().hex,
        host=host,
        profile=profile,
        tool=tool,
        effect=effect,
        op=op,
        transport=transport,
        args_hash=_args_hash(args),
        dry_run=dry_run,
    )


def _args_hash(args: dict[str, Any]) -> str:
    """Stable sha256 of the semantic args (canonical JSON)."""
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Approval:
    approved: bool
    approver: str | None = None
    reason: str | None = None


class _ApprovalForm(BaseModel):
    """The elicitation form a human fills to approve an act invocation."""

    approve: bool = False
    approver: str = "operator"


def _elicit_posture_on() -> bool:
    """Interactive elicitation is on unless the operator set KVM_PILOT_MCP_ELICIT=off."""
    return os.environ.get("KVM_PILOT_MCP_ELICIT", "").strip().lower() != "off"


def _client_supports_elicitation(ctx: Context | None) -> bool:
    if ctx is None:
        return False
    try:
        return ctx.session.check_client_capability(
            ClientCapabilities(elicitation=ElicitationCapability())
        )
    except Exception:  # noqa: BLE001 - a capability probe must never crash the tool
        return False


def _bound_state(effect: EffectClass) -> tuple[bool, bool]:
    """The mutable env state an approval is bound to: (dry_run, effect-gate-open).

    Re-checked after the human responds; a change invalidates the approval (the
    operator flipped dry-run off or revoked the gate while the call was paused).
    """
    return (env_flag("KVM_PILOT_MCP_DRY_RUN"), gate_enabled(effect))


async def approve_or_deny(ctx: Context | None, inv: InvocationContext, *, confirm: bool) -> Approval:
    """Run the two-guarantee approval for ``inv``. Never raises for a denial.

    Returns an :class:`Approval`; ``approved=False`` carries a ``reason`` the agent
    can act on. Only genuinely malformed states raise.
    """
    # Guarantee (a): the effect class must be operator-enabled.
    if not gate_enabled(inv.effect):
        flag = EFFECT_ENABLE_FLAG.get(inv.effect)
        return Approval(
            False,
            reason=(
                f"effect '{inv.effect}' is disabled on this server. Only the operator can "
                f"enable it, by setting {flag} in the server's own environment before "
                "starting it — it cannot be enabled from within an agent session."
            ),
        )

    before = _bound_state(inv.effect)

    # Guarantee (b): approved at run time — interactive elicitation or policy+confirm.
    if _elicit_posture_on() and _client_supports_elicitation(ctx):
        decision = await _elicit(ctx, inv)  # type: ignore[arg-type]
        if not decision.approved:
            return decision
        approver = decision.approver or "operator"
    else:
        # Pre-authorized / policy posture (unattended): standing enable-flag + confirm.
        if not confirm:
            return Approval(
                False,
                reason=(
                    "not approved: this client cannot prompt a human (no elicitation), so "
                    "an explicit confirm=true is required under the operator's standing "
                    "policy (the effect gate is the standing authorization)"
                ),
            )
        approver = "policy"

    # Invalidate if the bound env state changed while the human was deciding.
    if _bound_state(inv.effect) != before:
        return Approval(
            False, reason="approval invalidated: dry-run or the effect gate changed mid-approval"
        )
    return Approval(True, approver=approver)


async def _elicit(ctx: Context, inv: InvocationContext) -> Approval:
    """Prompt a human to approve this exact invocation. Same-path on any outcome."""
    message = (
        f"Approve {inv.tool} on host '{inv.host}'?\n"
        f"  effect={inv.effect}  transport={inv.transport}  op={inv.op}\n"
        f"  args_hash={inv.args_hash[:12]}  dry_run={inv.dry_run}  "
        f"invocation={inv.invocation_id[:8]}"
    )
    try:
        result = await ctx.elicit(message=message, schema=_ApprovalForm)
    except Exception as exc:  # noqa: BLE001 - a failed prompt is a recoverable denial
        return Approval(False, reason=f"approval prompt failed: {exc}")
    if result.action == "accept":
        data = result.data
        if data is not None and data.approve:
            return Approval(True, approver=data.approver or "operator")
        return Approval(False, reason="denied by approver")
    return Approval(False, reason=f"approval {result.action}")  # decline / cancel


# --------------------------------------------------------------------------- #
# Result shape (superset of _provenance; #72's receipt builds on it)          #
# --------------------------------------------------------------------------- #


def result(
    inv: InvocationContext,
    approval: Approval,
    *,
    detail: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The act-specific result fields (merge with ``_provenance(cfg)`` in the tool).

    Records both ``transport`` and ``effect`` — the anti-bypass invariant — plus a
    stable ``invocation_id`` and the (unsigned) approval binding tuple for #72.
    """
    out: dict[str, Any] = {
        "invocation_id": inv.invocation_id,
        "effect": str(inv.effect),
        "transport": inv.transport,
        "op": inv.op,
        "approved": approval.approved,
        "approval": {
            "approver": approval.approver,
            "profile": inv.profile,
            "args_hash": inv.args_hash,
            "dry_run": inv.dry_run,
            "effect": str(inv.effect),
            "expires": None,  # signed/expiring receipt deferred to #72
        },
        "detail": detail,
        "denied_reason": approval.reason,
    }
    if extra:
        out.update(extra)
    return out


__all__ = [
    "EFFECT_ENABLE_FLAG",
    "env_flag",
    "gate_enabled",
    "enforce_allowlist",
    "InvocationContext",
    "new_invocation",
    "Approval",
    "approve_or_deny",
    "result",
]
