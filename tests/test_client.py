"""Tests for KVMClient request dispatch and safety integration."""

import pytest

from kvm_pilot.client import KVMClient
from kvm_pilot.errors import SafetyError
from kvm_pilot.safety import allow_all, deny_all


def test_snapshot_hits_streamer(client, fake_http):
    fake_http.results["/api/streamer/snapshot"] = b"\xff\xd8jpeg"
    data = client.snapshot()
    assert data == b"\xff\xd8jpeg"
    assert "/api/streamer/snapshot" in fake_http.paths()


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
    fake_http.results["/api/streamer/snapshot"] = b"\xff\xd8jpeg"
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
