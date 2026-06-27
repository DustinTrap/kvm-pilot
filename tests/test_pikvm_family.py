"""GLKVM / BliKVM driver family — the PiKVMDriver split, GL API-disabled
detection, firmware info, and the quirk registry, over the real transport."""

from __future__ import annotations

import pytest

from emulator import EmulatorServer
from kvm_pilot import ApiDisabledError, BliKVMDriver, GLKVMDriver, KVMClient, PiKVMDriver
from kvm_pilot.errors import KVMPilotError


@pytest.fixture
def emu():
    with EmulatorServer() as server:
        yield server


def gl(emu, **kw) -> GLKVMDriver:
    return GLKVMDriver("127.0.0.1", "admin", "s3cr3t", port=emu.port, scheme="http", **kw)


# -- the family split ------------------------------------------------------

def test_family_are_pikvmdriver_subclasses():
    assert issubclass(GLKVMDriver, PiKVMDriver)
    assert issubclass(BliKVMDriver, PiKVMDriver)
    # KVMClient / PiKVMClient remain aliases of the canonical base.
    assert KVMClient is PiKVMDriver


def test_glkvm_capabilities_match_the_base(emu):
    # A fork advertises the same capabilities — it's the same kvmd API.
    assert gl(emu).capabilities() == PiKVMDriver("placeholder-host").capabilities()


# -- firmware tracking -----------------------------------------------------

def test_get_firmware_info_normalizes_version_and_model(emu):
    fw = gl(emu).get_firmware_info()
    assert fw["version"] == "4.2-gl-test"
    assert fw["kvmd_version"] == "4.2-gl-test"
    assert fw["model"] == "GL-RM1PE"


# -- GL API-disabled detection (the first-contact gotcha) ------------------

def test_check_api_enabled_passes_when_api_is_up(emu):
    info = gl(emu).check_api_enabled()
    assert info["hw"]["platform"]["model"] == "GL-RM1PE"


def test_api_disabled_surfaces_actionable_error(emu):
    emu.state.api_disabled = True  # GL firmware blocks /api/* -> 404
    with pytest.raises(ApiDisabledError) as ei:
        gl(emu).get_info()
    assert "nginx-kvmd.conf" in str(ei.value)


def test_check_api_enabled_detects_disabled(emu):
    emu.state.api_disabled = True
    with pytest.raises(ApiDisabledError):
        gl(emu).check_api_enabled()


def test_stock_pikvm_404_is_a_plain_error_not_apidisabled(emu):
    # The base driver has no GL hint, so a bare 404 from get_info stays generic
    # (only the explicit check_api_enabled() preflight upgrades it).
    emu.state.api_disabled = True
    base = PiKVMDriver("127.0.0.1", "admin", "s3cr3t", port=emu.port, scheme="http")
    with pytest.raises(KVMPilotError) as ei:
        base.get_info()
    assert not isinstance(ei.value, ApiDisabledError)
    assert ei.value.status_code == 404


# -- quirk registry --------------------------------------------------------

def test_known_quirks_includes_documented_api_disabled(emu):
    quirks = gl(emu).known_quirks()  # auto-detects firmware from the device
    by_id = {q.id: q for q in quirks}
    assert "api-disabled-by-default" in by_id
    assert by_id["api-disabled-by-default"].source == "documented"
    assert "nginx-kvmd.conf" in by_id["api-disabled-by-default"].workaround


def test_known_quirks_offline_with_explicit_firmware():
    # No network: pass a firmware string directly.
    quirks = GLKVMDriver("h").known_quirks(firmware="anything")
    assert any(q.id == "api-disabled-by-default" for q in quirks)
