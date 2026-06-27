"""
Safety layer for destructive KVM operations.

Two independent guards, both off by default in the *permissive* sense that a
plain library call still works — but the CLI turns confirmation on, and any
caller can opt into dry-run.

  * dry_run:  when True, destructive calls are logged and skipped, never sent
              to the device. Read-only calls always execute.
  * confirm:  a callback invoked before each destructive call. If it returns
              False, the call is blocked with SafetyError. The default callback
              allows everything (library default); the CLI installs an
              interactive y/N prompt, and --yes installs an allow-all.

A "destructive" operation is any one that can power-cycle, reset, wipe boot
media state, or otherwise change the target's running state in a way that is
not trivially reversible. The set is defined explicitly in DESTRUCTIVE_OPS so
it is auditable rather than guessed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .errors import SafetyError

logger = logging.getLogger("kvm_pilot.safety")

# Explicit, auditable list of operation identifiers considered destructive.
DESTRUCTIVE_OPS: set[str] = {
    "atx.power_off",
    "atx.power_off_hard",
    "atx.reset_hard",
    "atx.power_on",  # included: powering a box on is a state change worth gating
    "atx.click",
    "msd.set_params",
    "msd.connect",
    "msd.disconnect",
    "msd.remove_image",
    "msd.reset",
    "gpio.switch",
    "gpio.pulse",
    "redfish.power_action",
    # Redfish (BMC) driver — per-action granularity, mirroring the atx.*/msd.*
    # style. ``redfish.power_action`` (above) stays for the legacy KVMClient
    # Redfish helper; the RedfishDriver uses the specific ids below.
    "redfish.power_on",
    "redfish.power_off",
    "redfish.power_off_hard",
    "redfish.reset_hard",
    "redfish.virtual_media_insert",
    "redfish.virtual_media_eject",
    "hid.ctrl_alt_delete",
}

# Callback signature: (op_name, human_description) -> bool
ConfirmCallback = Callable[[str, str], bool]


def allow_all(_op: str, _desc: str) -> bool:
    """Default confirm callback: permit every operation."""
    return True


def deny_all(_op: str, _desc: str) -> bool:
    """Block every destructive operation (useful in tests / safe demos)."""
    return False


def interactive_confirm(op: str, desc: str) -> bool:
    """Prompt the operator on stdin. Returns True only on an explicit 'y'."""
    try:
        answer = input(f"[kvm-pilot] {desc}\n  Proceed? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


class SafetyPolicy:
    """Holds dry-run state and the confirmation callback for a client."""

    def __init__(
        self,
        dry_run: bool = False,
        confirm: ConfirmCallback | None = None,
    ):
        self.dry_run = dry_run
        self.confirm: ConfirmCallback = confirm or allow_all

    def guard(self, op: str, description: str) -> bool:
        """Evaluate guards for a destructive op.

        Returns True if the underlying call should proceed, False if it was
        intercepted by dry-run (caller should no-op). Raises SafetyError if the
        confirmation callback denied the operation.
        """
        if op not in DESTRUCTIVE_OPS:
            return True  # non-destructive ops are never gated

        if not self.confirm(op, description):
            raise SafetyError(f"Operation '{op}' was not confirmed: {description}")

        if self.dry_run:
            logger.warning("DRY-RUN: skipping destructive op '%s' (%s)", op, description)
            return False

        logger.info("Executing destructive op '%s' (%s)", op, description)
        return True


__all__ = [
    "SafetyPolicy",
    "DESTRUCTIVE_OPS",
    "allow_all",
    "deny_all",
    "interactive_confirm",
    "ConfirmCallback",
]
