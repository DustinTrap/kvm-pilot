"""Per-invocation authorization for the MCP act tools (issue #61).

Two guarantees per act call, from the Armorer Labs security review:

  (a) **allowed** — the effect gate is open (an operator env flag set in the
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

Approvals are consent RECEIPTS (#72): signed (per-process HMAC key), expiring
(``KVM_PILOT_MCP_RECEIPT_TTL``), single-use, re-verified against the exact
invocation immediately before dispatch, with a structured audit record per
terminal on the ``kvm_pilot.mcp.audit`` logger.

This module lives under ``mcp/`` (not core), so it may import ``mcp`` and
``pydantic``; it imports nothing from ``server`` to keep the dependency
one-directional (server -> act).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ClientCapabilities, ElicitationCapability
from pydantic import BaseModel

from kvm_pilot.safety import EffectClass

# --------------------------------------------------------------------------- #
# Guarantee (a): the effect class is operator-enabled                         #
# --------------------------------------------------------------------------- #

# Each effect class maps to its effect gate's env flag (None = never gated).
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
    EffectClass.APPLIANCE_RESET: "KVM_PILOT_MCP_ALLOW_APPLIANCE",
    EffectClass.EXTERNAL_WRITE: "KVM_PILOT_MCP_ALLOW_EXTERNAL_WRITE",
}


def env_flag(name: str) -> bool:
    """True if env var ``name`` is set to a truthy value (1/true/yes)."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def gate_enabled(effect: EffectClass) -> bool:
    """True if the operator enabled this effect class (or it needs no gate).

    An effect with no ``EFFECT_ENABLE_FLAG`` entry is fail-closed: a new effect
    class must get its own effect gate, never silently borrow another one
    (#190 — previously an unmapped effect fell back to the CONFIG flag).

    ``KVM_PILOT_MCP_READ_ONLY`` force-closes every effect gate regardless of
    the ``ALLOW_*`` flags (#196). In read-only mode the state-changing tools
    are not even registered; this is the independent second layer, so a
    registration bypass still fails closed instead of mutating.
    """
    if env_flag("KVM_PILOT_MCP_READ_ONLY"):
        return False
    if effect not in EFFECT_ENABLE_FLAG:
        return False
    flag = EFFECT_ENABLE_FLAG[effect]
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


def gate_summary() -> dict[str, bool]:
    """Effect-gate posture by friendly class name (#223).

    Deliberately no env-var names: the posture (which effects are enabled) is
    fine to report — an agent learns it piecemeal from refusals anyway — but the
    enabling incantation stays out-of-band, operator-only. Soft/hard power and
    HID input/control share flags, so they collapse to one name each.
    ``ssh`` and ``consent_off`` are direct-flag gates with no ``EffectClass``;
    the READ_ONLY force-close (#196) is replicated for them here.
    """
    read_only = env_flag("KVM_PILOT_MCP_READ_ONLY")
    return {
        "power": gate_enabled(EffectClass.POWER_SOFT),
        "hid": gate_enabled(EffectClass.HID_INPUT),
        "media": gate_enabled(EffectClass.MEDIA),
        "config": gate_enabled(EffectClass.CONFIG_MUTATION),
        "appliance": gate_enabled(EffectClass.APPLIANCE_RESET),
        "external_write": gate_enabled(EffectClass.EXTERNAL_WRITE),
        "ssh": not read_only and env_flag("KVM_PILOT_MCP_ALLOW_SSH"),
        "consent_off": not read_only and env_flag("KVM_PILOT_MCP_ALLOW_CONSENT_OFF"),
    }


def allowlist_names() -> list[str] | None:
    """The profile allowlist as data (None = no allowlist configured)."""
    if "KVM_PILOT_MCP_PROFILES" not in os.environ:
        return None
    return sorted(
        p.strip() for p in os.environ["KVM_PILOT_MCP_PROFILES"].split(",") if p.strip()
    )


def approval_posture() -> str:
    """How act approvals are decided on this server (#223, for `session`)."""
    return "interactive" if _elicit_posture_on() else "pre-authorized"


# --------------------------------------------------------------------------- #
# Guarantee (b): approved at run time                                         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class InvocationContext:
    """The invocation an approval binds to (Armorer's binding tuple).

    The signed/expiring single-use :class:`Receipt` (#72) is the HMAC over
    exactly these fields plus the approver + timestamps.
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


class Outcome(StrEnum):
    """Typed approval outcome (#149) — agents branch on this, not on the
    human-facing ``denied_reason`` strings.

    ``CANCELLED`` (a chat client killed the pending prompt — benign, retryable)
    is deliberately distinct from ``DENIED`` (the approver said no).
    """

    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"
    NOT_CONFIRMED = "not_confirmed"
    GATE_CLOSED = "gate_closed"
    INVALIDATED = "invalidated"


@dataclass(frozen=True)
class Approval:
    approved: bool
    approver: str | None = None
    reason: str | None = None
    remediation: str | None = None  # operator-facing fix for a client-side denial (#149)
    outcome: Outcome | None = None  # derived from `approved` when omitted

    def __post_init__(self) -> None:
        # approved and outcome are two views of one fact: derive the generic
        # outcome when omitted, and reject an inconsistent explicit pair —
        # Approval(True, outcome=DENIED) is a bug, not data.
        if self.outcome is None:
            object.__setattr__(
                self, "outcome", Outcome.APPROVED if self.approved else Outcome.DENIED
            )
        elif self.approved != (self.outcome is Outcome.APPROVED):
            raise ValueError(
                f"inconsistent Approval: approved={self.approved} outcome={self.outcome}"
            )


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
    can act on. Only genuinely malformed states raise. Every decision emits an
    audit record (#72).
    """
    approval = await _decide(ctx, inv, confirm=confirm)
    _audit_event(
        "approved" if approval.approved else "denied", inv, outcome=str(approval.outcome)
    )
    return approval


async def _decide(ctx: Context | None, inv: InvocationContext, *, confirm: bool) -> Approval:
    # Guarantee (a): the effect class must be operator-enabled. Stay-mum (#224):
    # never hand the agent the enabling env var to relay — the operator guide
    # (the server README) documents it out-of-band.
    if not gate_enabled(inv.effect):
        return Approval(
            False,
            reason=(
                f"effect '{inv.effect}' is disabled on this server. Only the operator "
                "can enable it, in the server's own environment before starting it "
                "(see the kvm-pilot MCP server README) — it cannot be enabled from "
                "within an agent session."
            ),
            outcome=Outcome.GATE_CLOSED,
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
                outcome=Outcome.NOT_CONFIRMED,
            )
        approver = "policy"

    # Invalidate if the bound env state changed while the human was deciding.
    if _bound_state(inv.effect) != before:
        return Approval(
            False,
            reason="approval invalidated: dry-run or the effect gate changed mid-approval",
            outcome=Outcome.INVALIDATED,
        )
    _clear_client_kills(inv.host)
    return Approval(True, approver=approver, outcome=Outcome.APPROVED)


# Issue #149: a chat client can kill an elicitation entirely client-side — a new
# chat message cancels the pending prompt ("approval cancel"), a mis-click denies
# it ("denied by approver") — while read-only tools keep working, so the denial
# reads as "the host is ignoring input". Name what happened and the operator's
# escape hatch in the result itself. Surfacing KVM_PILOT_MCP_ELICIT=off here does
# NOT leak a gate incantation (unlike the ALLOW_* refusals, which stay mum): the
# effect gate is checked before elicitation ever runs, and ELICIT only chooses the
# approval posture — it must still be set by the operator in the server's own env.
_ELICIT_TRADEOFF = (
    "If per-invocation approvals keep failing in this client, the operator can set "
    "KVM_PILOT_MCP_ELICIT=off in the MCP server's own environment and reconnect: the "
    "ALLOW_* effect gate plus per-call confirm=true then become the standing "
    "authorization. Trade-off: that disables per-call human approval, so it is the "
    "operator's decision, not a default recommendation."
)
_REMEDY_CANCELLED = (
    "The approval prompt was cancelled client-side before it was answered (in chat "
    "clients, sending a new message cancels a pending approval). The action never "
    "reached the device; read-only tools are unaffected. This is benign and "
    "retryable once the operator is ready to answer the prompt."
)
_REMEDY_DENIED = (
    "The approver declined this invocation client-side (a mis-click on the approval "
    "prompt does this too). The action never reached the device; read-only tools "
    "are unaffected. Retry only if the operator says it was accidental."
)

# Consecutive client-side approval kills per host (#149): the ELICIT=off
# trade-off is a real security decision, so it is surfaced only once a PATTERN
# emerges (>=2 in a row), not on every one-off mis-click. Module-level like
# _GENERATIONS — drivers are built per call, so instance state wouldn't survive.
_KILL_HINT_THRESHOLD = 2
_CLIENT_KILLS: dict[str, int] = {}


def _note_client_kill(host: str) -> int:
    with _gen_lock:
        _CLIENT_KILLS[host] = _CLIENT_KILLS.get(host, 0) + 1
        return _CLIENT_KILLS[host]


def _clear_client_kills(host: str) -> None:
    with _gen_lock:
        _CLIENT_KILLS.pop(host, None)


def _with_kill_hint(remedy: str, inv: InvocationContext) -> str:
    kills = _note_client_kill(inv.host)
    if kills < _KILL_HINT_THRESHOLD:
        return remedy
    return (
        f"{remedy} This is client-side approval failure #{kills} in a row on this "
        f"host. {_ELICIT_TRADEOFF}"
    )


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
        return Approval(
            False, reason=f"approval prompt failed: {exc}", outcome=Outcome.CANCELLED
        )
    if result.action == "accept":
        data = result.data
        if data is not None and data.approve:
            return Approval(True, approver=data.approver or "operator",
                            outcome=Outcome.APPROVED)
        return Approval(False, reason="denied by approver",
                        remediation=_with_kill_hint(_REMEDY_DENIED, inv),
                        outcome=Outcome.DENIED)
    # decline / cancel — both are client-side kills; cancelled is the benign one.
    if result.action == "cancel":
        return Approval(False, reason="approval cancel",
                        remediation=_with_kill_hint(_REMEDY_CANCELLED, inv),
                        outcome=Outcome.CANCELLED)
    return Approval(False, reason=f"approval {result.action}",
                    remediation=_with_kill_hint(_REMEDY_DENIED, inv),
                    outcome=Outcome.DENIED)


# --------------------------------------------------------------------------- #
# Approval receipts (#72): signed, expiring, single-use                       #
# --------------------------------------------------------------------------- #
#
# An approval authorizes exactly ONE dispatch of exactly ONE invocation. The
# receipt binds the full invocation tuple under an HMAC with a per-process
# ephemeral key (a restart voids receipts — correct for per-invocation scope),
# and the dispatch site re-verifies it immediately before acting: any bound
# field changed, expired, or already consumed -> fail closed with a
# denial-shaped result. The audit trail is structured LOG LINES on the
# dedicated ``kvm_pilot.mcp.audit`` logger (no file): every destructive
# invocation terminal — approved, denied, consumed, expired, mismatched,
# replayed, dispatch-exception — emits one JSON record with the invocation id.

_RECEIPT_KEY = secrets.token_bytes(32)
_RECEIPT_MAX_TRACKED = 512
_RECEIPTS: dict[str, str] = {}  # receipt_id -> issued | consumed | expired
_audit_log = logging.getLogger("kvm_pilot.mcp.audit")


def _receipt_ttl() -> float:
    """Receipt lifetime in seconds (``KVM_PILOT_MCP_RECEIPT_TTL``, default 60).

    Clamped to [1, 3600]; an unparseable value falls back to the default —
    a misconfigured TTL must not fail open (infinite) or closed (zero).
    """
    raw = os.environ.get("KVM_PILOT_MCP_RECEIPT_TTL", "").strip()
    if not raw:
        return 60.0
    try:
        return min(max(float(raw), 1.0), 3600.0)
    except ValueError:
        return 60.0


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    invocation_id: str
    approver: str
    issued_at: float
    expires_at: float
    mac: str


def _receipt_mac(inv: InvocationContext, approver: str, issued_at: float,
                 expires_at: float) -> str:
    payload = json.dumps(
        {
            "host": inv.host, "profile": inv.profile,
            "invocation_id": inv.invocation_id, "tool": inv.tool,
            "effect": str(inv.effect), "op": inv.op, "transport": inv.transport,
            "args_hash": inv.args_hash, "dry_run": inv.dry_run,
            "approver": approver, "issued_at": issued_at, "expires_at": expires_at,
        },
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return hmac.new(_RECEIPT_KEY, payload, hashlib.sha256).hexdigest()


def issue_receipt(inv: InvocationContext, approval: Approval) -> Receipt:
    """Mint the single-use receipt for an approved invocation."""
    issued_at = time.time()
    expires_at = issued_at + _receipt_ttl()
    approver = approval.approver or "operator"
    receipt = Receipt(
        receipt_id=uuid.uuid4().hex,
        invocation_id=inv.invocation_id,
        approver=approver,
        issued_at=issued_at,
        expires_at=expires_at,
        mac=_receipt_mac(inv, approver, issued_at, expires_at),
    )
    with _gen_lock:
        _RECEIPTS[receipt.receipt_id] = "issued"
        if len(_RECEIPTS) > _RECEIPT_MAX_TRACKED:  # bound growth: drop the oldest half
            for k in list(_RECEIPTS)[: len(_RECEIPTS) // 2]:
                del _RECEIPTS[k]
    _audit_event("issued", inv, receipt=receipt)
    return receipt


def verify_and_consume(
    receipt: Receipt, inv: InvocationContext, *, now: float | None = None
) -> str | None:
    """Re-verify the receipt against the invocation immediately before dispatch.

    Returns ``None`` when the receipt is valid (and marks it consumed — an
    approval authorizes exactly one dispatch), else the agent-facing denial
    reason. Fail-closed, never raises.
    """
    clock = time.time() if now is None else now
    with _gen_lock:
        state = _RECEIPTS.get(receipt.receipt_id)
        if state is None or receipt.invocation_id != inv.invocation_id:
            denial, event = (
                "receipt mismatched: not issued by this server for this "
                "invocation — re-approve", "mismatched",
            )
        elif state == "consumed":
            denial, event = (
                "receipt already consumed: an approval authorizes exactly one "
                "dispatch — re-approve to act again", "replayed",
            )
        elif not hmac.compare_digest(
            _receipt_mac(inv, receipt.approver, receipt.issued_at, receipt.expires_at),
            receipt.mac,
        ):
            denial, event = (
                "receipt mismatched: a bound field (host/tool/effect/args/dry-run) "
                "changed between approval and dispatch — re-approve", "mismatched",
            )
        elif clock >= receipt.expires_at:
            _RECEIPTS[receipt.receipt_id] = "expired"
            lifetime = receipt.expires_at - receipt.issued_at
            denial, event = (
                f"receipt expired: the approval outlived its "
                f"{lifetime:.0f}s lifetime — re-approve", "expired",
            )
        else:
            _RECEIPTS[receipt.receipt_id] = "consumed"
            denial, event = None, "consumed"
    _audit_event(event, inv, receipt=receipt)
    return denial


def receipt_state(receipt_id: str) -> str | None:
    with _gen_lock:
        return _RECEIPTS.get(receipt_id)


# Agent-readable act journal (#223): a bounded in-memory tail of *terminal*
# audit events, served by the `session` tool so a compacted session can ask
# "what has this server already done". The operator's `kvm_pilot.mcp.audit`
# logger stays the complete, authoritative trail; this is a convenience view.
# Same lifecycle as receipts: a server restart empties it, by design.
_JOURNAL: deque[dict[str, Any]] = deque(maxlen=50)
# "issued"/"approved" are interim states of the same invocation — journaling
# terminals only keeps it at one line per act.
_JOURNAL_EVENTS = frozenset(
    {"denied", "consumed", "expired", "mismatched", "replayed", "dispatch-exception"}
)


def journal_tail(limit: int = 15) -> list[dict[str, Any]]:
    """The most recent terminal act events, oldest first."""
    with _gen_lock:
        return list(_JOURNAL)[-limit:]


def journal_event(host: str, tool: str, op: str, *, dry_run: bool) -> None:
    """Journal a dispatch that bypasses the receipt pipeline (#223).

    ``power``/``ssh_exec``/``appliance_reboot``/``set_boot_device``/``amt_enable``
    predate the act layer (own gate + confirm, no receipts, no ``_audit_event``).
    Until they migrate onto it, this keeps the session journal complete;
    journal-only by design — the operator audit logger is the receipt
    pipeline's contract.
    """
    entry = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "event": "dispatched", "host": host, "tool": tool, "op": op,
        "effect": None, "outcome": "dispatched", "dry_run": dry_run,
        "invocation": None,
    }
    with _gen_lock:
        _JOURNAL.append(entry)


def _audit_event(
    event: str,
    inv: InvocationContext,
    *,
    receipt: Receipt | None = None,
    outcome: str | None = None,
    error: str | None = None,
) -> None:
    if event in _JOURNAL_EVENTS:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "event": event,
            "host": inv.host,
            "tool": inv.tool,
            "op": inv.op,
            "effect": str(inv.effect),
            "outcome": outcome or event,
            "dry_run": inv.dry_run,
            # Prefix only — enough to cross-reference the operator audit trail.
            "invocation": inv.invocation_id[:8],
        }
        with _gen_lock:
            _JOURNAL.append(entry)
    record: dict[str, Any] = {
        "event": event,
        "invocation_id": inv.invocation_id,
        "host": inv.host,
        "profile": inv.profile,
        "tool": inv.tool,
        "effect": str(inv.effect),
        "op": inv.op,
        "args_hash": inv.args_hash[:16],
        "dry_run": inv.dry_run,
    }
    if receipt is not None:
        record["receipt_id"] = receipt.receipt_id
        record["approver"] = receipt.approver
    if outcome is not None:
        record["outcome"] = outcome
    if error is not None:
        record["error"] = error
    _audit_log.info(json.dumps(record, sort_keys=True))


def audit_dispatch_error(inv: InvocationContext, receipt: Receipt, exc: Exception) -> None:
    """A dispatch that raised AFTER the receipt was consumed: the external
    effect is unknown — the audit trail must say so rather than staying silent."""
    _audit_event("dispatch-exception", inv, receipt=receipt, error=str(exc))


# --------------------------------------------------------------------------- #
# Result shape (superset of _provenance; #72's receipt builds on it)          #
# --------------------------------------------------------------------------- #


def result(
    inv: InvocationContext,
    approval: Approval,
    *,
    detail: str | None = None,
    extra: dict[str, Any] | None = None,
    receipt: Receipt | None = None,
) -> dict[str, Any]:
    """The act-specific result fields (merge with ``_provenance(cfg)`` in the tool).

    Records both ``transport`` and ``effect`` — the anti-bypass invariant — plus a
    stable ``invocation_id`` and, when a receipt was issued (#72), its id/state
    and the real approval expiry.
    """
    out: dict[str, Any] = {
        "invocation_id": inv.invocation_id,
        "effect": str(inv.effect),
        "transport": inv.transport,
        "op": inv.op,
        "approved": approval.approved,
        # #149: the typed outcome agents branch on; denied_reason stays the
        # human-facing text (cancelled = client-side kill, benign/retryable;
        # denied = the approver said no).
        "outcome": str(approval.outcome),
        "approval": {
            "approver": approval.approver,
            "profile": inv.profile,
            "args_hash": inv.args_hash,
            "dry_run": inv.dry_run,
            "effect": str(inv.effect),
            # #72: the signed receipt's real expiry (None when no receipt was
            # issued, i.e. the invocation was denied before one existed).
            "expires": (
                datetime.fromtimestamp(receipt.expires_at, UTC).isoformat()
                if receipt is not None else None
            ),
        },
        "detail": detail,
        "denied_reason": approval.reason,
        # #149: on a client-side elicitation denial, what happened + the operator fix.
        "remediation": approval.remediation,
    }
    if receipt is not None:
        out["receipt"] = {
            "id": receipt.receipt_id,
            "state": receipt_state(receipt.receipt_id),
        }
    if extra:
        out.update(extra)
    return out


# --------------------------------------------------------------------------- #
# Generation-keyed frame identity (mouse staleness, issue #124)               #
# --------------------------------------------------------------------------- #
#
# A per-host counter, bumped after any media/power effect. It is folded into the
# frame_ref an agent gets from `snapshot`, so a reference minted before a
# reboot/ISO-swap/retarget can't match one minted after (even if the pixels
# coincide) — an absolute mouse click can't land on a stale screen. Comparing the
# generation is a free in-memory check (no re-snapshot), and doesn't false-block
# on spinners/clocks/cursor blink the way a pixel hash would. In-memory only: a
# server restart resets it to 0, which conservatively invalidates old refs.

_GENERATIONS: dict[str, int] = {}
_gen_lock = threading.Lock()

# Effects that change the screen enough to invalidate a prior observation.
_GENERATION_BUMPING = (EffectClass.MEDIA, EffectClass.POWER_SOFT, EffectClass.POWER_HARD)


def generation(host: str) -> int:
    with _gen_lock:
        return _GENERATIONS.get(host, 0)


def bump_generation(host: str) -> int:
    with _gen_lock:  # read-modify-write is atomic under the lock
        _GENERATIONS[host] = _GENERATIONS.get(host, 0) + 1
        return _GENERATIONS[host]


def bumps_generation(effect: EffectClass) -> bool:
    return effect in _GENERATION_BUMPING


# When each frame_ref was minted (monotonic seconds) — the #141 content-age
# guard. Generation only bumps on media/power effects, so a frame_ref stays
# "valid" while the screen changes on its own (boot progresses, installer
# advances, a placeholder frame persists); an agent could carry a minutes-old
# observation into a click. This lets the mouse tool refuse a stale-by-age ref
# and any ref this server didn't issue. In-memory; cleared on restart (which,
# with the generation reset, conservatively invalidates prior observations).
_FRAME_MINTED: dict[str, float] = {}
_FRAME_MAX_TRACKED = 512


def frame_ref(host: str, image: bytes) -> str:
    """An observation token ``host:generation:shorthash`` for a captured frame.

    Minting stamps the frame's capture time (#141) so a later click can be
    refused if the observation is stale. An identical image re-mints the same
    ref and refreshes its timestamp — a genuinely-current static screen stays
    fresh; the "should have changed" case is caught separately by ``note_frame``.
    """
    ref = f"{host}:{generation(host)}:{hashlib.sha256(image).hexdigest()[:16]}"
    with _gen_lock:
        _FRAME_MINTED[ref] = time.monotonic()
        if len(_FRAME_MINTED) > _FRAME_MAX_TRACKED:  # bound growth: drop the oldest half
            cutoff = sorted(_FRAME_MINTED.values())[len(_FRAME_MINTED) // 2]
            for k in [k for k, v in _FRAME_MINTED.items() if v < cutoff]:
                del _FRAME_MINTED[k]
    return ref


def frame_age(ref: str) -> float | None:
    """Seconds since ``ref`` was minted by this server, or None if it never was."""
    with _gen_lock:
        minted = _FRAME_MINTED.get(ref)
    return None if minted is None else max(0.0, time.monotonic() - minted)


# Last wait_for_state result per host (#223): the resume breadcrumb for a
# multi-hour install — "you were waiting for installer_complete". In-memory
# only, deliberately: a restart voids all session state (receipts, generations,
# frame ages), and a persisted breadcrumb would be a confident-but-stale anchor.
_LAST_WAIT: dict[str, dict[str, Any]] = {}


def note_wait(host: str, record: dict[str, Any]) -> None:
    """Record the outcome of a wait_for_state call for the `session` tool."""
    with _gen_lock:
        _LAST_WAIT[host] = record


def last_wait(host: str) -> dict[str, Any] | None:
    with _gen_lock:
        return _LAST_WAIT.get(host)


# Last content hash returned per host — the #141 staleness tell: a byte-identical
# frame across a real screen change means the pixels are stale/cached, which the
# generation mechanism (media/power effects only) cannot catch.
_LAST_FRAME_HASH: dict[str, str] = {}


def note_frame(host: str, ref: str) -> bool:
    """Record a snapshot's content hash; True if byte-identical to the previous one."""
    content_hash = ref.rsplit(":", 1)[-1]
    with _gen_lock:
        same = _LAST_FRAME_HASH.get(host) == content_hash
        _LAST_FRAME_HASH[host] = content_hash
    return same


def frame_ref_generation(ref: str) -> int | None:
    """Parse the generation segment of a frame_ref, or None if malformed.

    ``rsplit(':', 2)`` so a host containing ':' (IPv6, host:port) still parses.
    """
    parts = ref.rsplit(":", 2)
    if len(parts) != 3:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def pct_to_kvmd(p: float) -> int:
    """Map a 0.0-1.0 screen fraction to kvmd's centered absolute axis (-32768..32767).

    Resolution-free: the kvmd absolute space already *is* a fraction of the screen,
    so a percentage coordinate survives a mode/resolution change (BIOS->GRUB->OS)
    that would invalidate a pixel coordinate.
    """
    p = max(0.0, min(1.0, p))
    return round(-32768 + p * 65535)


__all__ = [
    "EFFECT_ENABLE_FLAG",
    "env_flag",
    "gate_enabled",
    "enforce_allowlist",
    "InvocationContext",
    "new_invocation",
    "Approval",
    "Outcome",
    "Receipt",
    "approve_or_deny",
    "issue_receipt",
    "verify_and_consume",
    "receipt_state",
    "audit_dispatch_error",
    "result",
    "generation",
    "bump_generation",
    "bumps_generation",
    "frame_ref",
    "frame_ref_generation",
    "pct_to_kvmd",
]
