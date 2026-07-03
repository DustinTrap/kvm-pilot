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


def test_observed_atx_power_quirk_matches_its_firmware():
    # Observed on a GL-RM1PE running kvmd 4.82: ATX reports power='off' while the
    # host is booted. It is firmware-scoped, so it matches 4.82 but not others.
    on_482 = GLKVMDriver("h").known_quirks(firmware="4.82")
    by_id = {q.id: q for q in on_482}
    assert "atx-power-state-always-off" in by_id
    assert by_id["atx-power-state-always-off"].source == "observed"
    other = {q.id for q in GLKVMDriver("h").known_quirks(firmware="9.99")}
    assert "atx-power-state-always-off" not in other


# -- GL product firmware version (what the UI shows: /api/upgrade/version) --


class _FakeHTTP:
    """Minimal transport: return canned JSON per path, 404 for anything else."""

    def __init__(self, results):
        self.results = results

    def get(self, path, **kw):
        val = self.results.get(path)
        if val is None:
            from kvm_pilot.errors import KVMPilotError

            raise KVMPilotError("not found", 404)
        return val


def test_glkvm_reports_gl_product_firmware_when_available():
    d = GLKVMDriver("h")
    d._http = _FakeHTTP({
        "/api/info": {"system": {"kvmd": {"version": "4.82"},
                                 "platform": {"base": "Rockchip RV1126B-P EVB", "model": "v3"}}},
        "/api/upgrade/version": {"model": "RM1PE", "version": "V1.9.1 release1"},
    })
    fw = d.get_firmware_info()
    assert fw["version"] == "V1.9.1 release1"        # what the UI shows
    assert fw["product"] == "RM1PE" and fw["model"] == "RM1PE"
    assert fw["kvmd_version"] == "4.82" and fw["vendor"] == "gl.inet"


def test_glkvm_falls_back_to_kvmd_when_upgrade_endpoint_absent():
    d = GLKVMDriver("h")
    d._http = _FakeHTTP({
        "/api/info": {"system": {"kvmd": {"version": "4.82"},
                                 "platform": {"base": "some-board", "model": "v3"}}},
    })
    fw = d.get_firmware_info()
    assert fw["version"] == "4.82" and fw["product"] == "some-board"  # base identity


def test_glkvm_get_available_update_reports_drift():
    d = GLKVMDriver("h")
    d._http = _FakeHTTP({"/api/upgrade/compare": {
        "local_version": "V1.9.1 release1", "server_version": "V1.9.2 release1", "beta_version": ""}})
    assert d.get_available_update() == {
        "current": "V1.9.1 release1", "latest": "V1.9.2 release1",
        "beta": None, "update_available": True}


def test_glkvm_get_available_update_none_without_endpoint():
    d = GLKVMDriver("h")
    d._http = _FakeHTTP({})  # /api/upgrade/compare -> 404
    assert d.get_available_update() is None
