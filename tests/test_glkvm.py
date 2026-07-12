"""GLKVMDriver (the GL.iNet fork) — API-disabled detection, dual firmware
versions, the quirk registry, and the /api/upgrade/* flash path, over the real
transport. See src/kvm_pilot/drivers/glkvm.py for how the fork diverges from
stock PiKVM (#140)."""

from __future__ import annotations

import time

import pytest

from emulator import EmulatorServer
from kvm_pilot import GLKVMDriver
from kvm_pilot.client import PiKVMDriver
from kvm_pilot.drivers.base import Capability
from kvm_pilot.drivers.pikvm import BliKVMDriver
from kvm_pilot.errors import (
    ApiDisabledError,
    SafetyError,
    SnapshotFormatError,
    UnavailableError,
)
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


# -- on-demand-streamer snapshot recovery + keep-alive (#142) ---------------

class _FakeWS:
    """Minimal stand-in for a websocket-client WebSocket."""
    def __init__(self):
        self.closed = False
    def settimeout(self, _t):
        pass
    def recv(self):
        time.sleep(0.02)
        return "{}"          # a drained event frame
    def close(self):
        self.closed = True


def test_glkvm_snapshot_recovers_offline_streamer(emu, monkeypatch):
    # #142: a 503 with streamer_offline triggers a stream client, waits for the
    # encoder, and retries — returning a real frame and closing the client.
    d = gl(emu)
    snaps = {"n": 0}
    def fake_base_snapshot(self):
        snaps["n"] += 1
        if snaps["n"] == 1:
            raise UnavailableError("Service Unavailable", 503)
        return b"\xff\xd8\xff recovered"
    vsi = {"n": 0}
    def fake_vsi(self):
        vsi["n"] += 1
        return {"streamer_offline": vsi["n"] <= 1}   # offline first, up afterwards
    ws = _FakeWS()
    monkeypatch.setattr(PiKVMDriver, "snapshot", fake_base_snapshot)
    monkeypatch.setattr(GLKVMDriver, "video_signal_info", fake_vsi)
    monkeypatch.setattr(GLKVMDriver, "_connect_event_ws", lambda self, **kw: ws)
    assert d.snapshot().startswith(b"\xff\xd8\xff")
    assert ws.closed is True                          # trigger client cleaned up


def test_glkvm_snapshot_reraises_503_when_streamer_running(emu, monkeypatch):
    # A 503 while the streamer IS running is a different fault (wedge/reinit) —
    # do NOT open a trigger, just re-raise the honest error.
    d = gl(emu)
    monkeypatch.setattr(PiKVMDriver, "snapshot",
                        lambda self: (_ for _ in ()).throw(UnavailableError("503", 503)))
    monkeypatch.setattr(GLKVMDriver, "video_signal_info",
                        lambda self: {"streamer_offline": False})
    opened = {"ws": False}
    monkeypatch.setattr(GLKVMDriver, "_connect_event_ws",
                        lambda self, **kw: opened.__setitem__("ws", True))
    with pytest.raises(UnavailableError):
        d.snapshot()
    assert opened["ws"] is False                      # never triggered


def test_glkvm_snapshot_recovery_bails_on_h264(emu, monkeypatch):
    # #107/#151: once the streamer is up but emits H.264, waiting can't turn it
    # into JPEG — surface SnapshotFormatError promptly instead of spinning.
    d = gl(emu)
    snaps = {"n": 0}
    def fake_base_snapshot(self):
        snaps["n"] += 1
        if snaps["n"] == 1:
            raise UnavailableError("503", 503)
        raise SnapshotFormatError("non-JPEG bytes (H.264 NAL)")
    monkeypatch.setattr(PiKVMDriver, "snapshot", fake_base_snapshot)
    # offline on the first check (drives the trigger), up thereafter
    vsi = {"n": 0}
    monkeypatch.setattr(GLKVMDriver, "video_signal_info",
                        lambda self: {"streamer_offline": (vsi.__setitem__("n", vsi["n"] + 1) or vsi["n"] <= 1)})
    monkeypatch.setattr(GLKVMDriver, "_connect_event_ws", lambda self, **kw: _FakeWS())
    with pytest.raises(SnapshotFormatError):
        d.snapshot()
    assert snaps["n"] <= 3                             # bailed, did not spin the deadline


def test_glkvm_snapshot_falls_back_when_ws_missing(emu, monkeypatch):
    # Without the ws extra the trigger can't open — degrade to the honest 503.
    d = gl(emu)
    monkeypatch.setattr(PiKVMDriver, "snapshot",
                        lambda self: (_ for _ in ()).throw(UnavailableError("503", 503)))
    monkeypatch.setattr(GLKVMDriver, "video_signal_info",
                        lambda self: {"streamer_offline": True})
    def no_ws(self, **kw):
        raise ImportError("websocket-client is required")
    monkeypatch.setattr(GLKVMDriver, "_connect_event_ws", no_ws)
    with pytest.raises(UnavailableError):
        d.snapshot()


def test_streamer_warm_opens_and_closes_client(emu, monkeypatch):
    # The keep-alive holds a stream client for the block and closes it on exit.
    d = gl(emu)
    ws = _FakeWS()
    monkeypatch.setattr(GLKVMDriver, "_connect_event_ws", lambda self, **kw: ws)
    with d.streamer_warm(drain_interval=0.05):
        time.sleep(0.1)
    assert ws.closed is True


def test_streamer_warm_best_effort_when_ws_unavailable(emu, monkeypatch):
    # If the client can't be opened, warming is a no-op that never raises.
    d = gl(emu)
    monkeypatch.setattr(GLKVMDriver, "_connect_event_ws",
                        lambda self, **kw: (_ for _ in ()).throw(ImportError("no ws")))
    with d.streamer_warm():                            # must not raise
        pass


# ---- #187: MJPEG flip for a native-res JPEG -------------------------------- #


def _posts(emu, path):
    return [c for c in emu.state.calls if c == ("POST", path)]


def test_snapshot_flips_to_mjpeg_on_h264_and_restores(emu):
    # Over the real transport: H.264-at-native bytes fail the JPEG guard, the
    # driver flips video_format 0->1, the retry returns a JPEG at the SAME
    # resolution, and the prior format is restored for live video clients.
    emu.state.gl_video_format = 0
    emu.state.snapshot_h264_at_native = True
    d = gl(emu)
    assert d.snapshot().startswith(b"\xff\xd8\xff")
    assert emu.state.gl_video_format == 0              # restored after the shot
    assert len(_posts(emu, "/api/streamer/set_params")) == 2  # flip + restore


def test_snapshot_flip_gated_when_video_format_not_advertised(emu):
    # V1.5.1 exposes no params.video_format — no blind POST at unknown
    # firmware; the honest SnapshotFormatError surfaces (remediation: #177).
    emu.state.gl_video_format = None
    emu.state.snapshot_h264_at_native = True
    d = gl(emu)
    with pytest.raises(SnapshotFormatError):
        d.snapshot()
    assert not _posts(emu, "/api/streamer/set_params")
    # The "no video_format" fact is durable for the connection: a second
    # failing snapshot must not re-probe /api/streamer (memoized).
    probes = len([c for c in emu.state.calls if c == ("GET", "/api/streamer")])
    with pytest.raises(SnapshotFormatError):
        d.snapshot()
    assert len([c for c in emu.state.calls if c == ("GET", "/api/streamer")]) == probes


def test_snapshot_flip_reraises_when_already_mjpeg(emu, monkeypatch):
    # Bad bytes while the encoder is ALREADY MJPEG have some other cause —
    # flipping again can't help; the original error must surface unchanged.
    d = gl(emu)
    original = SnapshotFormatError("non-JPEG bytes")
    monkeypatch.setattr(PiKVMDriver, "snapshot",
                        lambda self: (_ for _ in ()).throw(original))
    monkeypatch.setattr(GLKVMDriver, "_streamer_params",
                        lambda self: {"video_format": 1})
    flips = []
    monkeypatch.setattr(GLKVMDriver, "_set_video_format",
                        lambda self, fmt: flips.append(fmt))
    with pytest.raises(SnapshotFormatError) as exc_info:
        d.snapshot()
    assert exc_info.value is original
    assert flips == []


def test_snapshot_flip_waits_out_encoder_reinit(emu, monkeypatch):
    # The switch re-inits the encoder; a transient 503 right after the flip is
    # part of the switch, not a wedge — retry within the bounded window.
    d = gl(emu)
    seq = iter([SnapshotFormatError("h264"), UnavailableError("503", 503)])
    def fake_base_snapshot(self):
        try:
            raise next(seq)
        except StopIteration:
            return b"\xff\xd8\xff after reinit"
    monkeypatch.setattr(PiKVMDriver, "snapshot", fake_base_snapshot)
    monkeypatch.setattr(GLKVMDriver, "_streamer_params",
                        lambda self: {"video_format": 0})
    flips = []
    monkeypatch.setattr(GLKVMDriver, "_set_video_format",
                        lambda self, fmt: flips.append(fmt))
    assert d.snapshot().startswith(b"\xff\xd8\xff")
    assert flips == [1, 0]                              # flip, then restore


def test_snapshot_flip_restore_failure_is_nonfatal(emu, monkeypatch):
    # A device drop between flip and restore leaves MJPEG set — benign and
    # logged; the successfully captured frame must still be returned.
    d = gl(emu)
    calls = {"n": 0}
    def fake_base_snapshot(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SnapshotFormatError("h264")
        return b"\xff\xd8\xff frame"
    def fragile_set(self, fmt):
        if fmt == 0:
            raise UnavailableError("gone", 503)
    monkeypatch.setattr(PiKVMDriver, "snapshot", fake_base_snapshot)
    monkeypatch.setattr(GLKVMDriver, "_streamer_params",
                        lambda self: {"video_format": 0})
    monkeypatch.setattr(GLKVMDriver, "_set_video_format", fragile_set)
    assert d.snapshot().startswith(b"\xff\xd8\xff")


def test_streamer_warm_holds_mjpeg_until_exit(emu, monkeypatch):
    # Inside streamer_warm() the flip is held: two H.264-failing snapshots
    # produce ONE flip and no mid-block restore; the restore fires at exit.
    emu.state.gl_video_format = 0
    emu.state.snapshot_h264_at_native = True
    d = gl(emu)
    monkeypatch.setattr(GLKVMDriver, "_connect_event_ws",
                        lambda self, **kw: _FakeWS())
    with d.streamer_warm(drain_interval=0.05):
        assert d.snapshot().startswith(b"\xff\xd8\xff")   # flips 0 -> 1
        assert emu.state.gl_video_format == 1             # held, not restored
        assert d.snapshot().startswith(b"\xff\xd8\xff")   # already MJPEG: clean
        assert len(_posts(emu, "/api/streamer/set_params")) == 1
    assert emu.state.gl_video_format == 0                 # restored at exit
    assert len(_posts(emu, "/api/streamer/set_params")) == 2


def test_stream_trigger_recovery_composes_with_mjpeg_flip(emu, monkeypatch):
    # The .39 failure shape end-to-end: streamer offline -> WS trigger warms the
    # encoder -> it emits H.264 at this res -> the MJPEG flip finishes the job.
    emu.state.gl_video_format = 0
    emu.state.snapshot_h264_at_native = True
    d = gl(emu)
    vsi = {"n": 0}
    def fake_vsi(self):
        vsi["n"] += 1
        return {"streamer_offline": vsi["n"] <= 1}       # offline first, then up
    seq = {"n": 0}
    real_snapshot = PiKVMDriver.snapshot
    def offline_then_real(self):
        seq["n"] += 1
        if seq["n"] == 1:
            raise UnavailableError("503", 503)           # cold streamer
        return real_snapshot(self)                       # emulator takes over
    monkeypatch.setattr(PiKVMDriver, "snapshot", offline_then_real)
    monkeypatch.setattr(GLKVMDriver, "video_signal_info", fake_vsi)
    monkeypatch.setattr(GLKVMDriver, "_connect_event_ws",
                        lambda self, **kw: _FakeWS())
    assert d.snapshot().startswith(b"\xff\xd8\xff")
    assert emu.state.gl_video_format == 0                 # flip + restore ran
    assert len(_posts(emu, "/api/streamer/set_params")) == 2


def test_known_quirks_includes_h264_native_res():
    d = GLKVMDriver("h", "u", "p")
    ids = {q.id for q in d.known_quirks(firmware="V1.9.1 release1")}
    assert "snapshot-h264-at-native-res" in ids
