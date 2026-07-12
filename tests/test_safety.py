"""Tests for the safety layer."""

import pytest

from kvm_pilot.errors import SafetyError
from kvm_pilot.safety import (
    DESTRUCTIVE_OPS,
    OP_EFFECT,
    EffectClass,
    SafetyPolicy,
    allow_all,
    deny_all,
    effect_of,
    shortcut_effect,
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


# -- effect taxonomy (additive over DESTRUCTIVE_OPS) --------------------------


def test_every_destructive_op_has_an_effect_class():
    # The receipt/gate layer must be able to classify every guarded op.
    missing = DESTRUCTIVE_OPS - set(OP_EFFECT)
    assert not missing, f"DESTRUCTIVE_OPS missing an EffectClass: {sorted(missing)}"


def test_effect_of_unmapped_op_is_observe():
    # Mirrors guard(): an op not in the map is a read, ungated.
    assert effect_of("info.get") is EffectClass.OBSERVE
    assert effect_of("power_state.read") is EffectClass.OBSERVE


def test_ctrl_alt_delete_is_power_soft_not_hid():
    # CAD is a reboot delivered over the keyboard — must not be ordinary HID.
    assert effect_of("hid.ctrl_alt_delete") is EffectClass.POWER_SOFT


def test_effect_classes_by_family():
    assert effect_of("hid.type_text") is EffectClass.HID_INPUT
    assert effect_of("hid.mouse_click") is EffectClass.HID_INPUT
    assert effect_of("msd.connect") is EffectClass.MEDIA
    assert effect_of("atx.power_on") is EffectClass.POWER_SOFT
    assert effect_of("atx.reset_hard") is EffectClass.POWER_HARD
    assert effect_of("gpio.pulse") is EffectClass.POWER_HARD
    assert effect_of("ssh.exec") is EffectClass.HID_CONTROL


@pytest.mark.parametrize(
    "keys,expected",
    [
        # Ctrl+Alt+Del — both modifier-side variants — is a soft reboot.
        ("ControlLeft,AltLeft,Delete", EffectClass.POWER_SOFT),
        ("ControlRight,AltRight,Delete", EffectClass.POWER_SOFT),
        ("altleft,controlleft,delete", EffectClass.POWER_SOFT),  # order/case insensitive
        # Magic SysRq reboot / poweroff are *harder* than CAD.
        ("AltLeft,PrintScreen,KeyB", EffectClass.POWER_HARD),
        ("AltLeft,PrintScreen,KeyO", EffectClass.POWER_HARD),
        ("AltLeft,SysRq,KeyB", EffectClass.POWER_HARD),
        # Other SysRq commands — soft.
        ("AltLeft,PrintScreen,KeyE", EffectClass.POWER_SOFT),
        ("AltLeft,PrintScreen,KeyS", EffectClass.POWER_SOFT),
        # Session shortcuts — hid_control, gated as HID not power.
        ("ControlLeft,AltLeft,F2", EffectClass.HID_CONTROL),  # VT switch
        ("AltLeft,F4", EffectClass.HID_CONTROL),  # Windows close
        ("ControlLeft,AltLeft,Backspace", EffectClass.HID_CONTROL),  # X restart
        # Unknown chord — loose default, still surfaced for sign-off.
        ("ControlLeft,KeyT", EffectClass.HID_CONTROL),
        ("MetaLeft,KeyL", EffectClass.HID_CONTROL),
    ],
)
def test_shortcut_effect_classifies_power_chord_families(keys, expected):
    assert shortcut_effect(keys) is expected


def test_file_firmware_report_is_external_write_not_device_destructive():
    # #190: filing a GitHub issue writes OUTSIDE the managed target — its own
    # effect class for the MCP gate, but NOT a device op in DESTRUCTIVE_OPS
    # (guard()/dry-run never see it; the gh helper owns its own dry-run).
    assert effect_of("report.file_firmware") is EffectClass.EXTERNAL_WRITE
    assert "report.file_firmware" not in DESTRUCTIVE_OPS
