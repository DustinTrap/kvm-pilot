"""Tests for KVMClient request dispatch and safety integration."""

import pytest

from kvm_pilot.client import KVMClient
from kvm_pilot.errors import SafetyError, SnapshotFormatError
from kvm_pilot.safety import allow_all, deny_all


def test_snapshot_hits_streamer(client, fake_http):
    fake_http.results["/api/streamer/snapshot"] = b"\xff\xd8\xffjpeg"
    data = client.snapshot()
    assert data == b"\xff\xd8\xffjpeg"
    assert "/api/streamer/snapshot" in fake_http.paths()


def test_snapshot_rejects_non_jpeg_bytes(client, fake_http):
    # Regression (#107): RM1PE firmware has returned raw H.264 with a JPEG
    # content type. Trusting the Content-Type feeds garbage to OCR/vision, so
    # non-JPEG bytes must raise a typed error, not be returned.
    fake_http.results["/api/streamer/snapshot"] = b"\x00\x00\x00\x01h264-nal"
    with pytest.raises(SnapshotFormatError) as ei:
        client.snapshot()
    assert "non-JPEG" in str(ei.value)


def _gl_streamer(*, hdmi_signal, captured_fps=0, jpeg_clients=False, width=1920,
                 height=1080, real_resolution=None):
    """A GL-shaped /api/streamer body (nested under 'streamer', with the hdmi
    block that source.online alone can't substitute for — the #154 case)."""
    source = {"online": True, "captured_fps": captured_fps,
              "resolution": {"width": width, "height": height}}
    if real_resolution is not None:
        source["real_resolution"] = real_resolution
    return {"streamer": {
        "hdmi": {"signal": hdmi_signal},
        "sinks": {"jpeg": {"has_clients": jpeg_clients}},
        "source": source,
    }}


def test_video_signal_info_reads_hdmi_signal_and_nulls_stale_geometry(client, fake_http):
    # #154: source.online is True but there is no picture; the authoritative
    # field is streamer.hdmi.signal. #158: the resolution dict then holds the
    # LAST-negotiated mode (stale), so geometry/fps must be nulled, not reported.
    fake_http.results["/api/streamer"] = _gl_streamer(hdmi_signal=False, captured_fps=196,
                                                      width=1600, height=900)
    info = client.video_signal_info()
    assert info["online"] is True and info["hdmi_signal"] is False
    assert info["width"] is None and info["height"] is None and info["fps"] is None
    assert info["streamer_idle"] is False  # 196fps spurious count -> not "idle"


def test_video_signal_info_reports_geometry_when_signal_present(client, fake_http):
    fake_http.results["/api/streamer"] = _gl_streamer(hdmi_signal=True, captured_fps=60,
                                                      width=2560, height=1440)
    info = client.video_signal_info()
    assert info["hdmi_signal"] is True
    assert info["width"] == 2560 and info["height"] == 1440 and info["fps"] == 60


def test_video_signal_info_flags_null_streamer_offline(client, fake_http):
    # #165: a null streamer block (on-demand idle, no subscriber) -> streamer_offline.
    fake_http.results["/api/streamer"] = {"streamer": None, "source": {}}
    assert client.video_signal_info()["streamer_offline"] is True
    fake_http.results["/api/streamer"] = _gl_streamer(hdmi_signal=True)
    assert client.video_signal_info()["streamer_offline"] is False


def test_video_signal_info_honors_real_resolution_no_signal(client, fake_http):
    # #158: V1.9.1 exposes source.real_resolution == "no_signal" — authoritative
    # even if hdmi.signal weren't there; the stale resolution dict is suppressed.
    fake_http.results["/api/streamer"] = _gl_streamer(hdmi_signal=True, real_resolution="no_signal",
                                                      width=1600, height=900)
    info = client.video_signal_info()
    assert info["width"] is None and info["height"] is None


def test_has_video_signal_prefers_hdmi_over_source_online(fake_http):
    # The core #154 fix: hdmi.signal False must win over source.online True.
    c = KVMClient("h")
    c._http = fake_http
    fake_http.results["/api/streamer"] = _gl_streamer(hdmi_signal=False)
    assert c.has_video_signal() is False
    fake_http.results["/api/streamer"] = _gl_streamer(hdmi_signal=True, captured_fps=30)
    assert c.has_video_signal() is True
    # No hdmi block but real_resolution says no_signal (#158) -> False.
    fake_http.results["/api/streamer"] = {"source": {"online": True, "real_resolution": "no_signal"}}
    assert c.has_video_signal() is False
    # Stock PiKVM (no hdmi block, no real_resolution): fall back to source.online.
    fake_http.results["/api/streamer"] = {"source": {"online": True}}
    assert c.has_video_signal() is True


class _Snapshot503HTTP:
    """Snapshot 503s; the streamer state endpoint still answers (the #142 field
    pattern: only the JPEG still path is failing)."""

    def __init__(self, streamer_state):
        self.streamer_state = streamer_state

    def get(self, path, **kw):
        from kvm_pilot.errors import KVMPilotError, UnavailableError

        if path == "/api/streamer/snapshot":
            raise UnavailableError("Subsystem unavailable (HTTP 503)", 503)
        if path == "/api/streamer":
            return self.streamer_state
        raise KVMPilotError("not found", 404)


def _snapshot_503_detail(streamer_state) -> str:
    from kvm_pilot.errors import UnavailableError

    c = KVMClient("h")
    c._http = _Snapshot503HTTP(streamer_state)
    with pytest.raises(UnavailableError) as ei:
        c.snapshot()
    return str(ei.value)


def test_snapshot_503_no_hdmi_signal_is_not_our_fault():
    # #154: hdmi.signal False -> "no video signal, not a kvm-pilot fault",
    # even though source.online is True.
    msg = _snapshot_503_detail(_gl_streamer(hdmi_signal=False))
    assert "no video signal" in msg and "not a kvm-pilot fault" in msg
    assert "hdmi_signal=False" in msg


def test_snapshot_503_idle_streamer_when_signal_present_no_subscriber():
    # #142: HDMI present, but the on-demand encoder is idle (fps 0, no client).
    msg = _snapshot_503_detail(_gl_streamer(hdmi_signal=True, captured_fps=0, jpeg_clients=False))
    assert "streamer is idle" in msg and "no subscriber" in msg


def test_snapshot_503_wedged_encoder_when_signal_present_and_capturing():
    # HDMI present AND capturing -> the JPEG path itself is wedged/re-initializing.
    msg = _snapshot_503_detail(_gl_streamer(hdmi_signal=True, captured_fps=30, jpeg_clients=True))
    assert "present and capturing" in msg and "wedged" in msg


def test_power_off_hard_gated_by_confirm(fake_http):
    c = KVMClient("fake", confirm=deny_all)
    c._http = fake_http
    with pytest.raises(SafetyError):
        c.power_off_hard()
    # Nothing should have been sent to the device.
    assert fake_http.calls == []


def test_dry_run_skips_send(fake_http):
    c = KVMClient("fake", dry_run=True, confirm=allow_all)
    c._http = fake_http
    c.reset_hard()
    assert fake_http.calls == []  # skipped


def test_power_on_sends_when_allowed(fake_http):
    c = KVMClient("fake", confirm=allow_all)
    c._http = fake_http
    c.power_on()
    assert "/api/atx/power" in fake_http.paths()


def test_type_text_sends_when_allowed(client, fake_http):
    # The client fixture has the library-default allow_all, so the gated call
    # still goes through — plain scripts keep working.
    client.type_text("root\n")
    assert "/api/hid/print" in fake_http.paths()


def test_hid_input_is_gated_by_dry_run(fake_http):
    c = KVMClient("fake", dry_run=True)
    c._http = fake_http
    c.type_text("rm -rf /\n")
    c.press_key("Enter")
    c.send_shortcut("ControlLeft,AltLeft,Delete")
    c.key_event("Escape", True)
    c.mouse_click()
    assert fake_http.calls == []  # nothing reaches the device under dry-run


def test_hid_input_is_gated_by_confirm(fake_http):
    c = KVMClient("fake", confirm=deny_all)
    c._http = fake_http
    for call in (
        lambda: c.type_text("x"),
        lambda: c.press_key("Enter"),
        lambda: c.send_shortcut("MetaLeft"),
        lambda: c.key_event("F2", True),
        lambda: c.mouse_click(),
    ):
        with pytest.raises(SafetyError):
            call()
    assert fake_http.calls == []


def test_type_text_guard_description_hides_the_text():
    # send_password routes through type_text: the confirm prompt/logs must never
    # contain the typed text itself, only its length.
    seen: list[str] = []

    def recording_confirm(op: str, desc: str) -> bool:
        seen.append(desc)
        return False

    c = KVMClient("fake", confirm=recording_confirm)
    with pytest.raises(SafetyError):
        c.type_text("hunter2")
    assert "hunter2" not in seen[0]
    assert "7 characters" in seen[0]


def test_ctrl_alt_delete_prompts_once_not_twice(fake_http):
    # ctrl_alt_delete has its own op id; it must not ALSO fire the
    # hid.send_shortcut guard (which would double-prompt an interactive user).
    seen: list[str] = []

    def recording_confirm(op: str, desc: str) -> bool:
        seen.append(op)
        return True

    c = KVMClient("fake", confirm=recording_confirm)
    c._http = fake_http
    c.ctrl_alt_delete()
    assert seen == ["hid.ctrl_alt_delete"]
    assert "/api/hid/events/send_shortcut" in fake_http.paths()


def test_press_key_releases_even_if_release_fails(fake_http):
    # If the up-event POST fails the key would stay held down on the target;
    # the client must attempt a HID reset and re-raise.
    class FlakyHTTP(type(fake_http)):
        def post(self, path, params=None, **kw):
            if params and params.get("state") == "false":
                super().post(path, params=params, **kw)  # record the attempt
                raise RuntimeError("boom")
            return super().post(path, params=params, **kw)

    flaky = FlakyHTTP()
    c = KVMClient("fake")
    c._http = flaky
    with pytest.raises(RuntimeError):
        c.press_key("Enter", hold_ms=0)
    states = [c["params"].get("state") for c in flaky.calls if c["path"].endswith("send_key")]
    assert states == ["true", "false"]  # release was attempted
    assert "/api/hid/reset" in flaky.paths()  # and the reset fallback fired


def test_msd_upload_streams_a_file_object_not_bytes(fake_http, tmp_path):
    # A multi-GB ISO must not be materialized in RAM: the body handed to the
    # transport must be a file object, with Content-Length pinned and no retry.
    iso = tmp_path / "x.iso"
    iso.write_bytes(b"BOOT" * 4096)
    c = KVMClient("fake", confirm=allow_all)
    c._http = fake_http
    c.msd_upload_file(str(iso))
    call = [c for c in fake_http.calls if c["path"] == "/api/msd/write"][0]
    assert not isinstance(call["body"], (bytes, bytearray))  # a stream, not bytes
    assert hasattr(call["body"], "read")
    assert call["extra_headers"]["Content-Length"] == str(iso.stat().st_size)
    assert call["retry"] is False


def test_msd_upload_file_dry_run_does_no_io(fake_http):
    # The guard fires before ANY I/O: under dry-run the (nonexistent) file is
    # never read and nothing is POSTed, so `mount --dry-run` really is a no-op.
    c = KVMClient("fake", dry_run=True)
    c._http = fake_http
    c.msd_upload_file("/does/not/exist.iso")
    assert fake_http.calls == []


def test_msd_upload_url_gated_by_confirm(fake_http):
    c = KVMClient("fake", confirm=deny_all)
    c._http = fake_http
    with pytest.raises(SafetyError):
        c.msd_upload_url("https://example.com/x.iso")
    assert fake_http.calls == []


def test_is_powered_on_fails_open_without_atx(client, fake_http):
    # No ATX board wired: kvmd reports enabled=false and the LEDs mean nothing —
    # power state is unknowable, so we must NOT report "off" (the vision layer
    # would short-circuit every classification to power_off).
    fake_http.results["/api/atx"] = {"enabled": False, "leds": {"power": False}}
    assert client.is_powered_on() is True
    fake_http.results["/api/atx"] = {"enabled": True, "leds": {"power": False}}
    assert client.is_powered_on() is False
    fake_http.results["/api/atx"] = {"leds": {"power": True}}  # no enabled key
    assert client.is_powered_on() is True


def test_snapshot_sends_no_quality_params(client, fake_http):
    # kvmd ignores preview_quality without preview=1 (and the preview would be
    # downscaled anyway) — the snapshot request must carry no params at all.
    fake_http.results["/api/streamer/snapshot"] = b"\xff\xd8\xffjpeg"
    client.snapshot()
    call = [c for c in fake_http.calls if c["path"] == "/api/streamer/snapshot"][0]
    assert not call.get("params")


def test_pixel_to_kvmd_maps_edges_exactly():
    from kvm_pilot.client import _pixel_to_kvmd

    assert _pixel_to_kvmd(0, 1920) == -32768
    assert _pixel_to_kvmd(1919, 1920) == 32767
    assert _pixel_to_kvmd(0, 1) == 0  # degenerate extent
    assert -40 < _pixel_to_kvmd(960, 1920) < 40  # ~screen center


def test_mouse_move_pixels_reads_resolution_and_maps(client, fake_http):
    fake_http.results["/api/streamer"] = {"source": {"resolution": {"width": 1920, "height": 1080}}}
    client.mouse_move_pixels(0, 1079)
    move = [c for c in fake_http.calls if c["path"] == "/api/hid/events/send_mouse_move"][0]
    assert move["params"] == {"to_x": -32768, "to_y": 32767}


def test_mount_iso_sequence(fake_http, tmp_path):
    iso = tmp_path / "x.iso"
    iso.write_bytes(b"data")
    c = KVMClient("fake", confirm=allow_all)
    c._http = fake_http
    fake_http.results["/api/msd"] = {"online": True}  # verify step (#77) sees it attach
    name = c.mount_iso(str(iso))
    assert name == "x.iso"
    paths = fake_http.paths()
    assert "/api/msd/write" in paths
    assert "/api/msd/set_params" in paths
    assert "/api/msd/set_connected" in paths


def test_msd_set_params_gated_by_confirm(fake_http):
    c = KVMClient("fake", confirm=deny_all)
    c._http = fake_http
    with pytest.raises(SafetyError):
        c.msd_set_params(image="x.iso")
    assert fake_http.calls == []  # boot-media selection must not fire when denied


def test_get_logs_follow_raises_capability_error(client, fake_http):
    # kvmd streams follow forever; the blocking transport can't serve it, so it
    # must refuse cleanly (never issue the request) rather than block to timeout.
    from kvm_pilot.errors import CapabilityError

    with pytest.raises(CapabilityError, match="follow"):
        client.get_logs(follow=True)
    assert fake_http.calls == []  # nothing was sent


def test_get_logs_without_follow_hits_api_log(client, fake_http):
    fake_http.results["/api/log"] = b"line1\nline2\n"
    assert client.get_logs() == "line1\nline2\n"
    assert "/api/log" in fake_http.paths()


def test_has_video_signal_reads_online_flag(client, fake_http):
    fake_http.results["/api/streamer"] = {"source": {"online": True}}
    assert client.has_video_signal() is True
    fake_http.results["/api/streamer"] = {"source": {"online": False}}
    assert client.has_video_signal() is False


def test_has_video_signal_handles_nested_and_unknown_shapes(client, fake_http):
    # Nested under "streamer" still resolves.
    fake_http.results["/api/streamer"] = {"streamer": {"source": {"online": False}}}
    assert client.has_video_signal() is False
    # Unknown shape must default to True so a real frame is never suppressed.
    fake_http.results["/api/streamer"] = {}
    assert client.has_video_signal() is True


def test_pikvmclient_alias():
    from kvm_pilot import PiKVMClient

    assert PiKVMClient is KVMClient


def test_from_config_applies_all_fields():
    from kvm_pilot.config import HostConfig

    cfg = HostConfig(
        host="box", user="u", passwd="p", port=8080, scheme="http",
        verify_ssl=True, timeout=5.0,
    )
    c = KVMClient.from_config(cfg, dry_run=True)
    # scheme/port/timeout must survive the helper (the bug the examples had was
    # dropping scheme and timeout in hand-rolled construction).
    assert c._http._base == "http://box:8080"
    assert c._http._timeout == 5.0
    assert c._http._verify_ssl is True
    assert c.safety.dry_run is True


# -- systematic safety-guard coverage (#52) --------------------------------
#
# guard() FAILS OPEN for op ids not in DESTRUCTIVE_OPS (safety.py), so a typo'd
# op id or a dropped guard() silently un-gates a destructive call. The deny-path
# table below is the regression net: with deny_all, a correctly-gated method
# raises SafetyError and sends nothing; a fail-open method would post, leaving
# fake_http.calls non-empty and failing the test.

def _client_with(fake_http, **kw) -> KVMClient:
    c = KVMClient("fake", **kw)
    c._http = fake_http
    return c


# (id, call, op_id, endpoint or None). endpoint=None => allow-path needs I/O we
# don't do here (msd_upload_file opens a file), so only deny/dry-run are checked.
_GATED_CLIENT = [
    ("power_on", lambda c: c.power_on(), "atx.power_on", "/api/atx/power"),
    ("power_off", lambda c: c.power_off(), "atx.power_off", "/api/atx/power"),
    ("power_off_hard", lambda c: c.power_off_hard(), "atx.power_off_hard", "/api/atx/power"),
    ("reset_hard", lambda c: c.reset_hard(), "atx.reset_hard", "/api/atx/power"),
    ("atx_click", lambda c: c.atx_click(), "atx.click", "/api/atx/click"),
    ("ctrl_alt_delete", lambda c: c.ctrl_alt_delete(), "hid.ctrl_alt_delete",
     "/api/hid/events/send_shortcut"),
    ("type_text", lambda c: c.type_text("x"), "hid.type_text", "/api/hid/print"),
    ("press_key", lambda c: c.press_key("Enter", hold_ms=0), "hid.press_key",
     "/api/hid/events/send_key"),
    ("send_shortcut", lambda c: c.send_shortcut("MetaLeft"), "hid.send_shortcut",
     "/api/hid/events/send_shortcut"),
    ("key_event", lambda c: c.key_event("F2", True), "hid.key_event",
     "/api/hid/events/send_key"),
    ("mouse_click", lambda c: c.mouse_click(hold_ms=0), "hid.mouse_click",
     "/api/hid/events/send_mouse_button"),
    ("msd_set_params", lambda c: c.msd_set_params(image="x"), "msd.set_params",
     "/api/msd/set_params"),
    ("msd_connect", lambda c: c.msd_connect(), "msd.connect", "/api/msd/set_connected"),
    ("msd_disconnect", lambda c: c.msd_disconnect(), "msd.disconnect",
     "/api/msd/set_connected"),
    ("msd_remove_image", lambda c: c.msd_remove_image("x"), "msd.remove_image",
     "/api/msd/remove"),
    ("msd_reset", lambda c: c.msd_reset(), "msd.reset", "/api/msd/reset"),
    ("msd_upload_url", lambda c: c.msd_upload_url("https://x/y.iso"), "msd.write_remote",
     "/api/msd/write_remote"),
    ("msd_upload_file", lambda c: c.msd_upload_file("/nonexistent.iso"), "msd.write", None),
    ("gpio_switch", lambda c: c.gpio_switch("r", True), "gpio.switch", "/api/gpio/switch"),
    ("gpio_pulse", lambda c: c.gpio_pulse("r"), "gpio.pulse", "/api/gpio/pulse"),
    ("redfish_power_action", lambda c: c.redfish_power_action("ForceOff"), "redfish.power_action",
     "/api/redfish/v1/Systems/0/Actions/ComputerSystem.Reset"),
]

_GATED_IDS = [e[0] for e in _GATED_CLIENT]
_GATED_WITH_ENDPOINT = [e for e in _GATED_CLIENT if e[3] is not None]


@pytest.mark.parametrize("_id,call,op_id,endpoint", _GATED_CLIENT, ids=_GATED_IDS)
def test_gated_client_method_blocks_on_deny(fake_http, _id, call, op_id, endpoint):
    with pytest.raises(SafetyError):
        call(_client_with(fake_http, confirm=deny_all))
    assert fake_http.calls == []  # a fail-open op would have posted


@pytest.mark.parametrize("_id,call,op_id,endpoint", _GATED_CLIENT, ids=_GATED_IDS)
def test_gated_client_method_skipped_under_dry_run(fake_http, _id, call, op_id, endpoint):
    call(_client_with(fake_http, dry_run=True, confirm=allow_all))
    assert fake_http.calls == []


@pytest.mark.parametrize("_id,call,op_id,endpoint", _GATED_WITH_ENDPOINT,
                         ids=[e[0] for e in _GATED_WITH_ENDPOINT])
def test_gated_client_method_uses_exact_op_and_endpoint(fake_http, _id, call, op_id, endpoint):
    seen: list[str] = []
    call(_client_with(fake_http, confirm=lambda op, desc: seen.append(op) or True))
    assert op_id in seen                 # pins the method to its exact op id
    assert endpoint in fake_http.paths()  # and the call actually went out


def test_table_op_ids_are_all_destructive():
    from kvm_pilot.safety import DESTRUCTIVE_OPS
    for _id, _call, op_id, _endpoint in _GATED_CLIENT:
        assert op_id in DESTRUCTIVE_OPS, f"{op_id} missing from DESTRUCTIVE_OPS"


def test_every_guard_literal_is_a_registered_destructive_op():
    # Fail-open guard: a guard("typo.op") would silently never gate. Scan the
    # source for every literal op id passed to .guard() and require it to be
    # registered. (Covers the PiKVM/Fake drivers' direct calls; the Redfish
    # driver passes ops via variables and is covered behaviorally above.)
    import re
    from pathlib import Path

    import kvm_pilot
    from kvm_pilot.safety import DESTRUCTIVE_OPS

    root = Path(kvm_pilot.__file__).parent
    pat = re.compile(r'\.guard\(\s*"([^"]+)"')
    used: set[str] = set()
    for py in root.rglob("*.py"):
        used |= set(pat.findall(py.read_text()))
    assert used  # sanity: the scan found guard() calls
    assert used <= DESTRUCTIVE_OPS, f"guard() op ids not registered: {used - DESTRUCTIVE_OPS}"


# -- broaden KVMClient public-surface coverage (#53) -----------------------

def test_check_auth_true_on_success(client, fake_http):
    assert client.check_auth() is True
    assert "/api/auth/check" in fake_http.paths()


def test_logout_clears_token(client, fake_http):
    client._http._auth_token = "tok"
    client.logout()
    assert "/api/auth/logout" in fake_http.paths()
    assert fake_http._auth_token is None


def test_get_logs_and_metrics_decode_raw(client, fake_http):
    fake_http.results["/api/log"] = b"line1\nline2\n"
    fake_http.results["/api/export/prometheus/metrics"] = b"kvmd_up 1\n"
    assert client.get_logs() == "line1\nline2\n"
    assert client.get_metrics() == "kvmd_up 1\n"


def test_snapshot_ocr_sends_region_and_decodes(client, fake_http):
    fake_http.results["/api/streamer/snapshot"] = b"GNU GRUB"
    text = client.snapshot_ocr(region=(1, 2, 3, 4))
    assert text == "GNU GRUB"
    call = [c for c in fake_http.calls if c["path"] == "/api/streamer/snapshot"][0]
    assert call["params"]["ocr"] == "true"
    assert call["params"]["ocr_left"] == 1 and call["params"]["ocr_bottom"] == 4


def test_state_getters_hit_their_endpoints(client, fake_http):
    for method, path in [
        (client.get_streamer_state, "/api/streamer"),
        (client.get_hid_state, "/api/hid"),
        (client.get_gpio_state, "/api/gpio"),
        (client.get_msd_state, "/api/msd"),
        (client.get_atx_state, "/api/atx"),
        (client.redfish_get_system, "/api/redfish/v1/Systems/0"),
    ]:
        method()
        assert path in fake_http.paths()


def test_reset_hid_and_set_params(client, fake_http):
    client.reset_hid()
    client.set_hid_params(keyboard_output="usb", jiggler=True)
    paths = fake_http.paths()
    assert "/api/hid/reset" in paths and "/api/hid/set_params" in paths
    sp = [c for c in fake_http.calls if c["path"] == "/api/hid/set_params"][0]
    assert sp["params"]["jiggler"] == "true"


def test_mouse_rel_and_scroll(client, fake_http):
    client.mouse_move_rel(3, -4)
    client.mouse_scroll(delta_y=-2)
    paths = fake_http.paths()
    assert "/api/hid/events/send_mouse_relative" in paths
    assert "/api/hid/events/send_mouse_wheel" in paths


def test_key_event_posts_state(client, fake_http):
    client.key_event("Escape", True)
    call = [c for c in fake_http.calls if c["path"] == "/api/hid/events/send_key"][0]
    assert call["params"]["state"] == "true"


def test_wait_for_power_state_returns_when_reached(client, fake_http):
    fake_http.results["/api/atx"] = {"enabled": True, "leds": {"power": True}}
    client.wait_for_power_state(True, timeout=1, poll=0.0)  # already on -> returns


def test_wait_for_power_state_times_out(client, fake_http):
    from kvm_pilot.errors import TimeoutError as KVMTimeoutError
    fake_http.results["/api/atx"] = {"enabled": True, "leds": {"power": False}}
    with pytest.raises(KVMTimeoutError):
        client.wait_for_power_state(True, timeout=0.05, poll=0.0)


def test_send_password_types_without_logging_secret(client, fake_http, caplog):
    import logging
    with caplog.at_level(logging.DEBUG):
        client.send_password("hunter2")
    assert "/api/hid/print" in fake_http.paths()
    assert "hunter2" not in caplog.text


def test_msd_upload_url_posts_write_remote(client, fake_http):
    client.msd_upload_url("https://srv/x.iso", image_name="x.iso")
    assert "/api/msd/write_remote" in fake_http.paths()


def test_enter_bios_cycles_then_spams_key(client, fake_http, monkeypatch):
    import kvm_pilot.client as cmod
    monkeypatch.setattr(cmod.time, "sleep", lambda *_: None)  # no real waits
    client.enter_bios(key="F2")
    paths = fake_http.paths()
    assert "/api/atx/power" in paths                       # hard_cycle
    assert "/api/hid/events/send_key" in paths             # key spam
