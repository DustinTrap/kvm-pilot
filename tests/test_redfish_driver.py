"""End-to-end tests for RedfishDriver over the real transport against a fake BMC.

Pure stdlib (see redfish_emulator.py): exercises hypermedia discovery, session
auth + logout, the ResetType mapping, safety gating, async tasks, sensors, logs,
and virtual media — no Docker, no hardware.
"""

from __future__ import annotations

import pytest

from kvm_pilot.drivers import RedfishDriver, make_driver
from kvm_pilot.drivers.base import Capability
from kvm_pilot.drivers.redfish.transport import RedfishHTTP
from kvm_pilot.errors import (
    AuthError,
    CapabilityError,
    ConnectionError,
    KVMPilotError,
    SafetyError,
)
from kvm_pilot.safety import deny_all
from redfish_emulator import RESET, SESSIONS, VM_EJECT, VM_INSERT

# The `emu` fixture (a running RedfishEmulator) is shared from tests/conftest.py.


def make(emu, **kw) -> RedfishDriver:
    return RedfishDriver("127.0.0.1", "admin", "secret", port=emu.port, scheme="http", **kw)


def _reset_posts(emu) -> list[dict]:
    return [body for path, body in emu.state.posts if path == RESET]


# -- capabilities + registry ----------------------------------------------

def test_capabilities_are_the_bmc_complementary_set(emu):
    caps = make(emu).capabilities()
    assert caps == {
        Capability.SYSTEM_INFO, Capability.POWER, Capability.BOOT_PROGRESS,
        Capability.SENSORS, Capability.LOGS, Capability.VIRTUAL_MEDIA,
    }
    # A BMC has no keyboard/mouse/screenshot/relay — these must be absent.
    for absent in (Capability.HID, Capability.VIDEO, Capability.GPIO,
                   Capability.EVENTS, Capability.SERIAL_CONSOLE, Capability.WATCHDOG):
        assert absent not in caps


def test_make_driver_redfish(emu):
    d = make_driver("redfish", host="127.0.0.1", port=emu.port, scheme="http", passwd="x")
    assert isinstance(d, RedfishDriver)


# -- discovery + info ------------------------------------------------------

def test_get_info_discovers_non_trivial_member(emu):
    info = make(emu).get_info()
    assert info["manufacturer"] == "ACME"
    assert info["model"] == "Server 9000"
    assert info["redfish_version"] == "1.15.1"
    # The system member is at /Systems/Self.1 (not "1") — discovery had to follow
    # @odata.id rather than assume an id.
    assert ("GET", "/redfish/v1/Systems/Self.1") in emu.state.calls


def test_get_info_fields_subset(emu):
    info = make(emu).get_info(fields=["model", "power_state"])
    assert set(info) == {"model", "power_state"}


# -- auth ------------------------------------------------------------------

def test_session_token_is_sent_and_deleted_on_close(emu):
    d = make(emu)
    d.get_info()
    # The emulator now rotates tokens per session; the driver must send the one
    # it was issued (first session -> tok-redfish-1).
    assert emu.state.last_headers.get("x-auth-token") == emu.state.valid_token
    assert emu.state.valid_token == "tok-redfish-1"
    assert any(path == "/redfish/v1/SessionService/Sessions" for _, path in emu.state.calls)
    d.close()
    assert emu.state.session_deleted is True


def test_password_change_required_raises_distinct_error(emu):
    emu.state.password_change_required = True
    with pytest.raises(AuthError, match="password change"):
        make(emu).get_info()


def test_basic_auth_mode_skips_session(emu):
    d = make(emu, auth="basic")
    d.get_info()
    assert not any(p == "/redfish/v1/SessionService/Sessions" for _, p in emu.state.calls)
    assert "authorization" in emu.state.last_headers


# -- power: ResetType mapping + gating ------------------------------------

def test_power_on_maps_to_on_and_waits(emu):
    d = make(emu)
    d.power_on()
    assert emu.state.power_state == "On"
    assert _reset_posts(emu) == [{"ResetType": "On"}]
    assert d.is_powered_on() is True


def test_power_off_prefers_graceful(emu):
    emu.state.power_state = "On"
    make(emu).power_off()
    assert _reset_posts(emu) == [{"ResetType": "GracefulShutdown"}]
    assert emu.state.power_state == "Off"


def test_power_off_hard_uses_force_off(emu):
    emu.state.power_state = "On"
    make(emu).power_off_hard()
    assert _reset_posts(emu) == [{"ResetType": "ForceOff"}]


def test_reset_hard_prefers_force_restart(emu):
    emu.state.power_state = "On"
    make(emu).reset_hard()
    assert _reset_posts(emu) == [{"ResetType": "ForceRestart"}]


def test_reset_type_mapping_respects_allowable_values(emu):
    # Target advertises only GracefulShutdown -> power_on has no candidate.
    emu.state.reset_allowable = ["GracefulShutdown"]
    with pytest.raises(CapabilityError, match="No supported ResetType"):
        make(emu).power_on()


def test_dry_run_skips_the_reset_post(emu):
    make(emu, dry_run=True).power_on()
    assert _reset_posts(emu) == []          # no destructive POST
    assert emu.state.power_state == "Off"   # state untouched


def test_deny_confirm_blocks_and_sends_nothing(emu):
    with pytest.raises(SafetyError):
        make(emu, confirm=deny_all).power_on()
    assert _reset_posts(emu) == []


# iDRAC8-class set: no GracefulShutdown, so power_off's preference falls to
# PushPowerButton — a state toggle that must not be fired when already off.
_IDRAC8_RESET = ["On", "ForceOff", "GracefulRestart", "PushPowerButton", "Nmi"]


def test_power_off_when_already_off_is_a_noop(emu):
    # The core #42 regression: on iDRAC8 (PushPowerButton preferred over ForceOff
    # for off), power_off on an already-off host must NOT pulse the button (which
    # would power it ON) — it must issue zero resets.
    emu.state.reset_allowable = _IDRAC8_RESET
    emu.state.power_state = "Off"
    make(emu).power_off()
    assert _reset_posts(emu) == []            # no reset issued
    assert emu.state.power_state == "Off"     # host stays off, not toggled on


def test_power_off_on_idrac8_uses_pushpowerbutton_toward_off(emu):
    # When the host IS on, PushPowerButton is the correct graceful-ish choice on
    # iDRAC8, and the toggle moves it to Off.
    emu.state.reset_allowable = _IDRAC8_RESET
    emu.state.power_state = "On"
    make(emu).power_off()
    assert _reset_posts(emu) == [{"ResetType": "PushPowerButton"}]
    assert emu.state.power_state == "Off"


def test_power_on_when_already_on_is_a_noop(emu):
    emu.state.power_state = "On"
    make(emu).power_on()
    assert _reset_posts(emu) == []
    assert emu.state.power_state == "On"


def test_reset_rejected_but_state_at_target_is_success(emu):
    # Vendor non-idempotence / race: the BMC 409s the reset but the host is at
    # the target state anyway — treat as success, don't raise.
    emu.state.power_state = "On"
    emu.state.reset_reject_status = 409
    make(emu, max_retries=0).power_off()  # must not raise
    assert _reset_posts(emu) == [{"ResetType": "GracefulShutdown"}]
    assert emu.state.power_state == "Off"


def test_reset_rejected_and_not_at_target_still_raises(emu):
    # A genuine failure (rejected AND not at target) must still surface.
    emu.state.power_state = "On"
    emu.state.reset_reject_status = 400
    emu.state.reset_allowable = ["GracefulRestart"]  # power_off has no candidate...
    # ...so instead force a reject where the state never reaches target: use a
    # restart intent that 400s. reset_hard has target_on=None, so the 400 is not
    # swallowed and must propagate.
    with pytest.raises(KVMPilotError):
        make(emu, max_retries=0).reset_hard()


def test_async_reset_polls_task(emu):
    emu.state.reset_async = True  # Reset returns 202 + a Task that Completes
    d = make(emu)
    d.power_on()
    assert emu.state.power_state == "On"
    assert ("GET", "/redfish/v1/TaskService/Tasks/1") in emu.state.calls


# -- boot progress ---------------------------------------------------------

@pytest.mark.parametrize("last_state,expected", [
    ("OSRunning", "os_running"),
    ("SetupEntered", "bios_menu"),
    ("OSBootStarted", "booting"),
    ("MemoryInitializationStarted", "post_screen"),
    ("SomethingNew", "unknown"),
])
def test_boot_progress_maps_to_phase_vocabulary(emu, last_state, expected):
    emu.state.boot_progress = last_state
    assert make(emu).get_boot_progress() == expected


def test_boot_progress_none_while_off_is_power_off(emu):
    emu.state.boot_progress = "None"
    emu.state.power_state = "Off"
    assert make(emu).get_boot_progress() == "power_off"


@pytest.mark.parametrize("power_state", ["PoweringOn", "PoweringOff", "Paused"])
def test_boot_progress_transitional_power_is_unknown_not_off(emu, power_state):
    # DSP0268 PowerState has transitional values; only a literal "Off" may be
    # reported as power_off (a wait loop must not think a mid-transition host
    # is down).
    emu.state.boot_progress = "None"
    emu.state.power_state = power_state
    assert make(emu).get_boot_progress() == "unknown"


# -- logs ------------------------------------------------------------------

def test_get_logs_pages_and_renders(emu):
    text = make(emu).get_logs()
    lines = text.splitlines()
    assert len(lines) == 2
    assert "system booted" in text
    assert "Warning" in text  # MessageSeverity fallback parsed


def test_get_logs_seek(emu):
    assert len(make(emu).get_logs(seek=1).splitlines()) == 1


def test_logs_follow_raises(emu):
    with pytest.raises(CapabilityError, match="tail-follow"):
        make(emu).get_logs(follow=True)


# -- sensors ---------------------------------------------------------------

def test_sensors_legacy_thermal_power(emu):
    s = make(emu).read_sensors()
    assert s["temperatures"][0]["reading"] == 42
    assert s["fans"][0]["reading"] == 4200
    assert s["power"][0]["reading"] == 210


def test_sensors_unified_model(emu):
    emu.state.sensors_mode = "unified"
    s = make(emu).read_sensors()
    assert any(t["name"] == "CPU Temp" for t in s["temperatures"])
    assert any(f["name"] == "Fan1" for f in s["fans"])


# -- virtual media ---------------------------------------------------------

def test_mount_iso_inserts_and_returns_name(emu):
    name = make(emu).mount_iso("http://srv/imgs/ubuntu-24.04.iso?sig=x")
    assert name == "ubuntu-24.04.iso"
    assert emu.state.inserted is True
    assert emu.state.last_image == "http://srv/imgs/ubuntu-24.04.iso?sig=x"
    assert any(p == VM_INSERT for p, _ in emu.state.posts)


def test_msd_disconnect_ejects(emu):
    d = make(emu)
    d.mount_iso("http://srv/x.iso")
    d.msd_disconnect()
    assert emu.state.inserted is False
    assert any(p == VM_EJECT for p, _ in emu.state.posts)


def test_virtual_media_gated_dry_run(emu):
    make(emu, dry_run=True).mount_iso("http://srv/x.iso")
    assert emu.state.inserted is False
    assert not any(p == VM_INSERT for p, _ in emu.state.posts)


def test_virtual_media_deny_raises(emu):
    with pytest.raises(SafetyError):
        make(emu, confirm=deny_all).mount_iso("http://srv/x.iso")


# -- transport: retry + redaction -----------------------------------------

def test_transport_retries_transient_503(emu):
    emu.state.fail_status = 503
    emu.state.fail_times = 2  # first two attempts fail, transport retries
    assert make(emu).get_info()["manufacturer"] == "ACME"


def test_redaction_hides_password_and_token():
    http = RedfishHTTP("h", "admin", "s3cr3t", auth="basic")
    http._token = "tok-abc"
    redacted = http._redact("login s3cr3t with tok-abc failed")
    assert "s3cr3t" not in redacted
    assert "tok-abc" not in redacted


# -- review-driven regressions --------------------------------------------

def test_get_info_reflects_fresh_power_state(emu):
    # Volatile fields must not be served from the frozen ComputerSystem cache.
    d = make(emu)
    assert d.get_info()["power_state"] == "Off"
    d.power_on()
    assert d.get_info()["power_state"] == "On"


def test_session_teardown_falls_back_to_body_odata_id(emu):
    emu.state.session_send_location = False  # non-compliant BMC: no Location header
    d = make(emu)
    d.get_info()
    d.close()
    assert emu.state.session_deleted is True  # DELETE still issued via body @odata.id


def test_async_task_gc_404_is_treated_as_success(emu):
    emu.state.reset_async = True
    emu.state.task_gc = True  # iDRAC/iLO retire the finished task -> monitor 404s
    d = make(emu)
    d.power_on()  # must not raise
    assert emu.state.power_state == "On"


def test_async_task_critical_status_raises(emu):
    emu.state.reset_async = True
    emu.state.task_status = "Critical"  # Completed-but-failed
    with pytest.raises(KVMPilotError):
        make(emu).power_on()


def test_logs_resolve_via_hypermedia_link_not_concat(emu):
    # The emulator's LogServices link points to a NON-"/LogServices" path, so this
    # only passes if the driver follows the advertised @odata.id link.
    assert "system booted" in make(emu).get_logs()


def test_mount_iso_usb_threads_cdrom_flag(emu):
    # cdrom=False prefers removable media; with only a CD slot present it falls
    # back to it, but the flag must be honored (not silently ignored).
    name = make(emu).mount_iso("http://srv/disk.img", cdrom=False)
    assert name == "disk.img"
    assert emu.state.inserted is True


def _session_posts(emu) -> int:
    return sum(1 for path, _ in emu.state.posts if path == SESSIONS)


def test_reauthenticates_once_when_session_expires_midflight(emu):
    # A real BMC idle-times-out the session (DSP0266 SessionService timeout).
    # The next request 401s with a token attached; the driver must re-login once
    # and retry transparently, not fail permanently.
    d = make(emu)
    d.get_info()                       # establishes session #1
    assert _session_posts(emu) == 1
    emu.state.expire_token_once = True  # session #1's token is now stale
    info = d.get_info()                 # must transparently recover
    assert info["power_state"] == "Off"
    assert _session_posts(emu) == 2     # exactly one re-login
    # and the driver is healthy afterwards (no further re-logins needed)
    d.is_powered_on()
    assert _session_posts(emu) == 2


def test_recovers_after_close_then_reuse(emu):
    # After close() the token is gone but the discovery caches survive, so the
    # next call goes out unauthenticated and 401s. The transport must re-login.
    d = make(emu)
    d.get_info()
    d.close()
    assert emu.state.session_deleted is True
    assert d.get_info()["power_state"] == "Off"   # reuse must not fail
    assert _session_posts(emu) == 2               # a fresh session was created


def test_reauth_does_not_loop_on_bad_credentials(emu):
    # If re-login also fails (e.g. credentials revoked), the AuthError must
    # surface after exactly one retry, not spin.
    d = make(emu)
    d.get_info()
    emu.state.expire_token_once = True
    emu.state.password_change_required = True  # the re-login POST will now fail
    with pytest.raises(AuthError):
        d.get_info()


def test_credentials_pinned_to_configured_origin():
    http = RedfishHTTP("bmc.lan", "u", "pw", port=443, scheme="https")
    assert http._same_origin("/redfish/v1/Systems") is True          # relative
    assert http._same_origin("https://bmc.lan/redfish/v1/x") is True  # implicit 443
    assert http._same_origin("https://bmc.lan:443/redfish/v1/x") is True
    assert http._same_origin("https://evil.example/x") is False       # other host
    assert http._same_origin("http://bmc.lan/x") is False             # scheme differs


def _serve_redfish(handler_cls):
    import http.server
    import threading

    srv = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_redfish_redirect_is_refused_and_token_never_forwarded():
    # A BMC that 302s off-origin must not cause the session token to be copied
    # to the redirect target (the _same_origin guard only covers URLs we build;
    # this covers server-issued redirects).
    import http.server

    seen: list[dict] = []

    class Sink(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    sink, sink_port = _serve_redfish(Sink)

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sink_port}/stolen")
            self.end_headers()

        def log_message(self, *a):
            pass

    redir, redir_port = _serve_redfish(Redirector)
    try:
        http = RedfishHTTP("127.0.0.1", "root", "s3cr3t", scheme="http",
                           port=redir_port, auth="basic", max_retries=0)
        http._token = "session-token-xyz"  # simulate a live session
        with pytest.raises(ConnectionError) as ei:
            http.request("GET", "/redfish/v1/Systems/0")
        assert "redirect" in str(ei.value).lower()
        assert seen == []  # the off-origin sink never saw X-Auth-Token / Basic
    finally:
        sink.shutdown()
        redir.shutdown()
