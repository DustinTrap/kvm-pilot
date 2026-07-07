"""GLKVMDriver (the GL.iNet fork) — API-disabled detection, dual firmware
versions, the quirk registry, and the /api/upgrade/* flash path, over the real
transport. See src/kvm_pilot/drivers/glkvm.py for how the fork diverges from
stock PiKVM (#140)."""

from __future__ import annotations

import pytest

from emulator import EmulatorServer
from kvm_pilot import GLKVMDriver
from kvm_pilot.client import PiKVMDriver
from kvm_pilot.drivers.base import Capability
from kvm_pilot.drivers.pikvm import BliKVMDriver
from kvm_pilot.errors import ApiDisabledError, SafetyError
from kvm_pilot.safety import deny_all


@pytest.fixture
def emu():
    with EmulatorServer() as server:
        yield server


def gl(emu, **kw) -> GLKVMDriver:
    return GLKVMDriver("127.0.0.1", "admin", "s3cr3t", port=emu.port, scheme="http", **kw)


# -- capability delta over the base ----------------------------------------

def test_glkvm_adds_firmware_update_over_the_base(emu):
    # The fork speaks the same kvmd API PLUS GL's /api/upgrade/* remote-flash
    # surface, so it advertises exactly one extra capability: FIRMWARE_UPDATE.
    base = PiKVMDriver("placeholder-host").capabilities()
    assert Capability.FIRMWARE_UPDATE not in base
    assert gl(emu).capabilities() == base | {Capability.FIRMWARE_UPDATE}


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


# -- host-visible virtual-media device name (#78) ---------------------------


def test_glkvm_declares_host_visible_vmedia_name():
    # Observed on a real GL-RM1PE (#78): the Dell T7610 F12 boot menu listed
    # "UEFI: Glinet Optical Drive 1.00" exactly when /api/msd flipped
    # online=true. A substring (the "1.00" USB revision may vary), not a regex.
    assert GLKVMDriver("h").virtual_media_host_pattern == "Glinet Optical Drive"


def test_vmedia_host_name_unset_for_unobserved_brands():
    # The #78 table rows for stock PiKVM and BliKVM stay blank until real
    # hardware fills them — never invent unobserved device data.
    assert PiKVMDriver("h").virtual_media_host_pattern is None
    assert BliKVMDriver("h").virtual_media_host_pattern is None


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


def test_quirk_autodetect_matches_kvmd_version_behind_product_version():
    # Regression (#139): on real GL hardware get_firmware_info() reports the GL
    # *product* firmware ("V1.9.1 release1") as `version`, while the ATX quirk is
    # keyed to the kvmd component version ("4.82"). Auto-detection must match
    # against both, or the quirk — and the healthcheck warning built on it —
    # silently disappears exactly on the hardware it was observed on.
    d = GLKVMDriver("h")
    d._http = _FakeHTTP({
        "/api/info": {"system": {"kvmd": {"version": "4.82"},
                                 "platform": {"base": "Rockchip RV1126B-P EVB", "model": "v3"}}},
        "/api/upgrade/version": {"model": "RM1PE", "version": "V1.9.1 release1"},
    })
    ids = {q.id for q in d.known_quirks()}
    assert "atx-power-state-always-off" in ids


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


# -- remote firmware update (FirmwareUpdate capability) --------------------


def test_get_upgrade_status_aggregates_the_read_endpoints():
    d = GLKVMDriver("h")
    d._http = _FakeHTTP({
        "/api/upgrade/status": {"enabled": True},
        "/api/upgrade/version": {"model": "RM1PE", "version": "V1.5.1 release2"},
        "/api/upgrade/download": {"size": 307581578},
    })
    assert d.get_upgrade_status() == {
        "enabled": True, "current": "V1.5.1 release2",
        "model": "RM1PE", "image_size": 307581578}


def test_get_upgrade_status_disabled_when_subsystem_absent():
    d = GLKVMDriver("h")
    d._http = _FakeHTTP({})  # every /api/upgrade/* -> 404 (older firmware)
    assert d.get_upgrade_status() == {"enabled": False}


def test_apply_firmware_update_dry_run_sends_nothing(emu):
    emu.state.upgrade_present = True
    res = gl(emu).apply_firmware_update(dry_run=True)
    assert res["sent"] is False and res["dry_run"] is True
    # The whole point: no POST reached the device.
    assert ("POST", "/api/upgrade/start") not in emu.state.calls


def test_apply_firmware_update_executes_start_when_confirmed(emu):
    emu.state.upgrade_present = True
    res = gl(emu).apply_firmware_update(dry_run=False)  # default confirm = allow_all
    assert res["sent"] is True
    assert res["verified"] == "upgrade-state"  # not just a 200 on start (#94)
    assert ("POST", "/api/upgrade/start") in emu.state.calls


def test_apply_firmware_update_reports_failure_when_start_noops(emu):
    # Regression (#94): on a real RM1PE POST /api/upgrade/start can 200 and do
    # nothing. Success requires an observed state transition, not a returned call.
    emu.state.upgrade_present = True
    emu.state.upgrade_start_noop = True
    res = gl(emu).apply_firmware_update(
        dry_run=False, verify_timeout=0.2, poll_interval=0.02)
    assert res["sent"] is False
    assert "did not enter upgrade state" in res["error"]
    assert ("POST", "/api/upgrade/start") in emu.state.calls  # it tried
    assert "result" in res  # raw start response surfaced for the operator


def test_apply_firmware_update_denied_raises_and_sends_nothing(emu):
    emu.state.upgrade_present = True
    with pytest.raises(SafetyError):
        gl(emu, confirm=deny_all).apply_firmware_update(dry_run=False)
    assert ("POST", "/api/upgrade/start") not in emu.state.calls


def test_apply_firmware_update_uploads_local_image_before_flashing(emu, tmp_path):
    emu.state.upgrade_present = True
    img = tmp_path / "rm1pe.img"
    img.write_bytes(b"\x00" * 4096)
    res = gl(emu).apply_firmware_update(image=str(img), dry_run=False)
    assert res["sent"] is True
    posts = [p for (m, p) in emu.state.calls if m == "POST"]
    # Upload must precede the flash trigger.
    assert posts.index("/api/upgrade/upload") < posts.index("/api/upgrade/start")
