"""Tests for the safety layer."""

import pytest

from kvm_pilot.errors import SafetyError
from kvm_pilot.safety import (
    DESTRUCTIVE_OPS,
    SafetyPolicy,
    allow_all,
    deny_all,
)


def test_nondestructive_op_always_proceeds():
    policy = SafetyPolicy(dry_run=True, confirm=deny_all)
    # An op not in the destructive set is never gated, even with deny_all + dry_run.
    assert policy.guard("info.get", "read info") is True


def test_dry_run_skips_destructive():
    policy = SafetyPolicy(dry_run=True, confirm=allow_all)
    assert policy.guard("atx.power_off_hard", "hard off") is False


def test_confirm_denied_raises():
    policy = SafetyPolicy(dry_run=False, confirm=deny_all)
    with pytest.raises(SafetyError):
        policy.guard("atx.reset_hard", "reset")


def test_confirm_allowed_executes():
    policy = SafetyPolicy(dry_run=False, confirm=allow_all)
    assert policy.guard("atx.power_on", "power on") is True


def test_destructive_set_includes_power_and_media():
    for op in ("atx.power_off_hard", "atx.reset_hard", "msd.connect", "gpio.switch"):
        assert op in DESTRUCTIVE_OPS


def test_dry_run_short_circuits_before_confirmation():
    # Dry-run wins: the op is logged and skipped without consulting confirm, so
    # --dry-run never prompts and works unattended (even with a denying callback).
    policy = SafetyPolicy(dry_run=True, confirm=deny_all)
    assert policy.guard("atx.power_off", "off") is False


def test_dry_run_never_invokes_confirm():
    calls: list[str] = []

    def recording_confirm(op: str, desc: str) -> bool:
        calls.append(op)
        return True

    policy = SafetyPolicy(dry_run=True, confirm=recording_confirm)
    assert policy.guard("atx.power_off_hard", "hard off") is False
    assert calls == []


def test_hid_and_msd_write_ops_are_destructive():
    for op in (
        "hid.type_text",
        "hid.press_key",
        "hid.send_shortcut",
        "hid.key_event",
        "hid.mouse_click",
        "msd.write",
        "msd.write_remote",
    ):
        assert op in DESTRUCTIVE_OPS
