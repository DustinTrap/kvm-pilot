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


def test_type_text_not_gated(client, fake_http):
    client.type_text("root\n")
    assert "/api/hid/print" in fake_http.paths()


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


def test_pikvmclient_alias():
    from kvm_pilot import PiKVMClient

    assert PiKVMClient is KVMClient
