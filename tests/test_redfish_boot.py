"""RedfishDriver boot-device control (BootSourceOverride) — #28/#201.

Exercised over the real transport against the fake BMC (redfish_emulator), incl.
iLO4/iDRAC7-era quirks: no BootSourceOverrideMode field, a BMC that rejects the
mode property, async (202) PATCH, and restricted AllowableValues.
"""

from __future__ import annotations

import pytest

from kvm_pilot.drivers import RedfishDriver
from kvm_pilot.drivers.base import BootConfig, Capability
from kvm_pilot.errors import CapabilityError, KVMPilotError, SafetyError
from kvm_pilot.safety import deny_all
from redfish_emulator import SYS


def make(emu, **kw) -> RedfishDriver:
    return RedfishDriver("127.0.0.1", "admin", "secret", port=emu.port, scheme="http", **kw)


def _boot_patches(emu) -> list[dict]:
    return [body["Boot"] for path, body in emu.state.patches if path == SYS and "Boot" in body]


# -- capability + protocol -------------------------------------------------

def test_driver_satisfies_boot_config_capability(emu):
    d = make(emu)
    assert isinstance(d, BootConfig)
    assert Capability.BOOT_CONFIG in d.capabilities()


# -- reporting -------------------------------------------------------------

def test_get_boot_options_default_disabled(emu):
    opts = make(emu).get_boot_options()
    assert opts["enabled"] == "Disabled"
    assert opts["once"] is False and opts["persistent"] is False
    assert opts["target"] == "none"
    assert opts["mode"] == "UEFI"
    assert opts["mode_settable"] is True
    # normalized allowable, sorted
    assert opts["allowable"] == sorted(["none", "pxe", "cd", "hdd", "usb", "bios", "diag"])


# -- set: one-time / persistent / clear -----------------------------------

def test_set_pxe_once_patches_and_reports(emu):
    opts = make(emu).set_boot_device("pxe", once=True)
    patch = _boot_patches(emu)[-1]
    assert patch == {
        "BootSourceOverrideEnabled": "Once",
        "BootSourceOverrideTarget": "Pxe",
        "BootSourceOverrideMode": "UEFI",
    }
    assert emu.state.boot_override_enabled == "Once"
    assert emu.state.boot_override_target == "Pxe"
    assert opts["once"] is True and opts["target"] == "pxe"


def test_set_cd_persistent(emu):
    make(emu).set_boot_device("cd", once=False)
    patch = _boot_patches(emu)[-1]
    assert patch["BootSourceOverrideEnabled"] == "Continuous"
    assert patch["BootSourceOverrideTarget"] == "Cd"
    assert emu.state.boot_override_enabled == "Continuous"


def test_set_none_clears_override(emu):
    emu.state.boot_override_enabled = "Once"
    emu.state.boot_override_target = "Pxe"
    make(emu).set_boot_device("none")
    patch = _boot_patches(emu)[-1]
    assert patch["BootSourceOverrideEnabled"] == "Disabled"
    assert patch["BootSourceOverrideTarget"] == "None"
    # clearing never sends a mode
    assert "BootSourceOverrideMode" not in patch
    assert emu.state.boot_override_enabled == "Disabled"


def test_legacy_mode(emu):
    make(emu).set_boot_device("hdd", uefi=False)
    assert _boot_patches(emu)[-1]["BootSourceOverrideMode"] == "Legacy"
    assert emu.state.boot_override_mode == "Legacy"


@pytest.mark.parametrize(
    "token,expected",
    [("dvd", "Cd"), ("disk", "Hdd"), ("setup", "BiosSetup"), ("BIOS", "BiosSetup"),
     ("Pxe", "Pxe"), ("diag", "Diags")],
)
def test_device_token_aliases(emu, token, expected):
    make(emu).set_boot_device(token)
    assert _boot_patches(emu)[-1]["BootSourceOverrideTarget"] == expected


# -- validation ------------------------------------------------------------

def test_unknown_device_raises(emu):
    with pytest.raises(KVMPilotError):
        make(emu).set_boot_device("floppy")
    assert _boot_patches(emu) == []  # never PATCHed


def test_target_not_in_allowable_raises_capability_error(emu):
    # A restricted BMC (no PXE) must fail fast, not send an opaque 400.
    emu.state.boot_allowable = ["None", "Hdd", "BiosSetup"]
    with pytest.raises(CapabilityError):
        make(emu).set_boot_device("pxe")
    assert _boot_patches(emu) == []


# -- safety gating ---------------------------------------------------------

def test_dry_run_does_not_patch(emu):
    opts = make(emu, dry_run=True).set_boot_device("pxe")
    assert _boot_patches(emu) == []            # gated, no write
    assert emu.state.boot_override_enabled == "Disabled"
    assert opts["target"] == "none"            # reports unchanged state


def test_denied_confirm_raises_and_does_not_patch(emu):
    with pytest.raises(SafetyError):
        make(emu, confirm=deny_all).set_boot_device("pxe")
    assert _boot_patches(emu) == []


# -- iLO4 / iDRAC7 quirks --------------------------------------------------

def test_no_mode_field_omits_mode(emu):
    # Older iLO4/iDRAC7: ComputerSystem.Boot has no BootSourceOverrideMode.
    emu.state.boot_expose_mode = False
    opts = make(emu).set_boot_device("pxe")
    patch = _boot_patches(emu)[-1]
    assert "BootSourceOverrideMode" not in patch      # never sent
    assert patch["BootSourceOverrideTarget"] == "Pxe"
    assert opts["mode_settable"] is False


def test_mode_rejected_retries_without_mode(emu):
    # A BMC that 400s a PATCH containing BootSourceOverrideMode: the driver must
    # retry once without it and still apply the target.
    emu.state.boot_patch_rejects_mode = True
    make(emu).set_boot_device("cd")
    patches = _boot_patches(emu)
    assert len(patches) == 2                          # first with mode, retry without
    assert "BootSourceOverrideMode" in patches[0]
    assert "BootSourceOverrideMode" not in patches[1]
    assert emu.state.boot_override_target == "Cd"     # applied on retry


def test_async_patch_is_awaited(emu):
    # BMC returns 202 + Task for the PATCH; driver polls it to completion.
    emu.state.boot_patch_status = 202
    make(emu).set_boot_device("usb")
    assert emu.state.boot_override_target == "Usb"
    assert ("GET", "/redfish/v1/TaskService/Tasks/1") in emu.state.calls


def test_restricted_allowable_reported_normalized(emu):
    emu.state.boot_allowable = ["None", "Pxe", "Hdd"]
    opts = make(emu).get_boot_options()
    assert opts["allowable"] == ["hdd", "none", "pxe"]


# -- error / edge paths ----------------------------------------------------

def test_set_boot_device_async_task_failure_raises(emu):
    emu.state.boot_patch_status = 202
    emu.state.task_state = "Exception"          # a failed terminal TaskState
    with pytest.raises(KVMPilotError):
        make(emu).set_boot_device("pxe")


def test_set_boot_device_non_mode_patch_failure_propagates(emu):
    # A 500 (or any failure that is NOT the mode-property 400) must propagate,
    # never be swallowed as the mode-retry case.
    emu.state.boot_patch_fail_status = 500
    with pytest.raises(KVMPilotError):
        make(emu).set_boot_device("cd")


def test_set_boot_device_survives_session_expiry(emu):
    # BMC drops the session mid-flow (DSP0266 idle timeout); the transport
    # re-authenticates once and the override still applies.
    emu.state.expire_token_once = True
    make(emu).set_boot_device("hdd")
    assert emu.state.boot_override_target == "Hdd"


def test_get_boot_options_when_no_boot_object(emu):
    # A minimal BMC exposing no ComputerSystem.Boot: sane defaults, no mode.
    emu.state.boot_absent = True
    opts = make(emu).get_boot_options()
    assert opts["enabled"] == "Disabled"
    assert opts["target"] == "none"
    assert opts["mode_settable"] is False
    assert opts["allowable"] == []
