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


def test_dry_run_still_requires_confirmation():
    # Confirmation is evaluated before dry-run, so a denied op raises even in dry-run.
    policy = SafetyPolicy(dry_run=True, confirm=deny_all)
    with pytest.raises(SafetyError):
        policy.guard("atx.power_off", "off")
