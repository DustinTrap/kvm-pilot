"""AmtDriver unit tests, driven against the pure-stdlib WS-Man emulator.

Documents the AMT WS-Man contract: capability detection, the CIM power-state
mapping, single-use boot override, inventory, safety gating, and the
digest-auth / SOAP-fault paths. Everything stays on 127.0.0.1 (enforced by the
autouse network guard in conftest).
"""

from __future__ import annotations

import pytest

from kvm_pilot.drivers.amt import AmtDriver
from kvm_pilot.drivers.amt.wsman import WsmanError
from kvm_pilot.drivers.base import Capability
from kvm_pilot.errors import (
    AuthError,
    CapabilityError,
    ConnectionError,
    KVMPilotError,
    ProtocolError,
    SafetyError,
)
from kvm_pilot.errors import TimeoutError as KpTimeoutError
from kvm_pilot.safety import deny_all


def make(amt_emu, **kw) -> AmtDriver:
    # tls defaults False -> http:// against the emulator's ephemeral loopback port.
    return AmtDriver("127.0.0.1", "admin", "secret", port=amt_emu.port, confirm=lambda *_: True, **kw)


# -- capabilities ---------------------------------------------------------


def test_capabilities(amt_emu):
    caps = make(amt_emu).capabilities()
    # The full stack: WS-Man power/inventory/boot, SOL serial, RFB video + HID.
    assert {
        Capability.POWER, Capability.SYSTEM_INFO, Capability.BOOT_CONFIG,
        Capability.SERIAL_CONSOLE, Capability.VIDEO, Capability.HID,
    } <= caps


# -- power ----------------------------------------------------------------


def test_is_powered_on_reads_cim_powerstate(amt_emu):
    drv = make(amt_emu)
    amt_emu.state.power_state = "2"
    assert drv.is_powered_on() is True
    amt_emu.state.power_state = "8"
    assert drv.is_powered_on() is False


@pytest.mark.parametrize(
    "method,expected",
    [("power_on", "2"), ("power_off", "8"), ("power_off_hard", "6"), ("reset_hard", "10")],
)
def test_power_actions_map_to_cim_codes(amt_emu, method, expected):
    getattr(make(amt_emu), method)()
    assert amt_emu.state.last_power_request == expected
    assert ("RequestPowerStateChange", "CIM_PowerManagementService") in amt_emu.state.calls


def test_power_dry_run_sends_nothing(amt_emu):
    make(amt_emu, dry_run=True).power_off_hard()
    assert amt_emu.state.last_power_request is None
    assert amt_emu.state.calls == []


def test_power_deny_raises_and_sends_nothing(amt_emu):
    drv = AmtDriver("127.0.0.1", "admin", "secret", port=amt_emu.port, confirm=deny_all)
    with pytest.raises(SafetyError):
        drv.power_on()
    assert amt_emu.state.last_power_request is None


def test_soap_fault_becomes_wsman_error(amt_emu):
    drv = make(amt_emu)
    amt_emu.state.fault_reason = "power package not supported"
    with pytest.raises(WsmanError):
        drv.power_on()


def test_http_error_surfaces_soap_fault_reason(amt_emu):
    """An HTTP 4xx/5xx whose body is a SOAP fault is surfaced with its Reason and
    specific Subcode, not truncated to the XML namespace preamble (#216).

    Regression from a live ME-firmware-update incident: the wedged ME answered
    every call with ``HTTP 500 / e:TimedOut`` and the old ``body[:300]`` slice
    discarded the reason, leaving only ``<?xml … xmlns:g=… xmlns:f=…``."""
    from kvm_pilot.drivers.amt.wsman import _S

    amt_emu.state.http_status = 500
    amt_emu.state.error_body = (
        f'<s:Envelope xmlns:s="{_S}"><s:Body><s:Fault>'
        "<s:Code><s:Value>s:Receiver</s:Value>"
        "<s:Subcode><s:Value>e:TimedOut</s:Value></s:Subcode></s:Code>"
        "<s:Reason><s:Text>The operation has timed out.</s:Text></s:Reason>"
        "</s:Fault></s:Body></s:Envelope>"
    )
    with pytest.raises(WsmanError) as ei:
        make(amt_emu).power_on()
    msg = str(ei.value)
    assert "The operation has timed out." in msg
    assert "e:TimedOut" in msg          # the specific subcode, not the Sender/Receiver class
    assert "xmlns" not in msg           # not the raw, truncated namespace preamble


# -- system info ----------------------------------------------------------


def test_get_info_identity(amt_emu):
    info = make(amt_emu).get_info()
    assert info["manufacturer"] == "Dell Inc."
    assert info["model"] == "Latitude 5411"
    assert info["serial_number"] == "REDACTED"
    assert info["amt_version"] == "16.1.25"
    assert info["provisioning_state"] == "post"
    assert info["power_state"] in ("on", "off")


def test_get_info_fields_subset(amt_emu):
    info = make(amt_emu).get_info(fields=["model", "power_state"])
    assert set(info) == {"model", "power_state"}


def test_firmware_info_feeds_health_label(amt_emu):
    assert make(amt_emu).get_firmware_info()["version"] == "16.1.25"


def test_firmware_info_has_vendor_product(amt_emu):
    # The run ledger / firmware registry join on vendor+product — a bare version
    # records identity as fake/fake (test-report bug the standard now forbids).
    fw = make(amt_emu).get_firmware_info()
    assert fw["vendor"] == "Dell Inc."
    assert fw["product"] == "Latitude 5411"
    assert fw["version"] == "16.1.25"


def test_get_info_survives_partial_failure(amt_emu):
    # A firmware that faults must not blank every field — get_info is best-effort.
    amt_emu.state.fault_reason = "boom"
    info = make(amt_emu).get_info()
    assert set(info) == {
        "manufacturer", "model", "serial_number", "uuid",
        "amt_version", "provisioning_state", "power_state",
    }
    assert info["power_state"] == "off"  # is_powered_on failed -> reported off


# -- boot config ----------------------------------------------------------


def test_set_boot_pxe_changes_order_single_use(amt_emu):
    make(amt_emu).set_boot_device("pxe")
    assert "Force PXE Boot" in amt_emu.state.boot_order
    methods = [a for a, _ in amt_emu.state.calls]
    assert "ChangeBootOrder" in methods
    assert "SetBootConfigRole" in methods  # made single-use


def test_set_boot_bios_sets_biossetup(amt_emu):
    make(amt_emu).set_boot_device("bios")
    assert amt_emu.state.bios_setup == "true"


def test_set_boot_bios_firmware_rejection_is_clear(amt_emu, monkeypatch):
    # Some firmware (Latitude 5411, AMT 14.1.67) rejects BIOSSetup=true with an
    # opaque 400 InvalidRepresentation though pxe/cd work — the driver must turn
    # that into a clear CapabilityError, not a raw WsmanError (#215).
    drv = make(amt_emu)
    orig_put = drv._wsman.put

    def fake_put(uri, body, selectors=None):
        if "BootSettingData" in uri and "BIOSSetup>true" in body:
            raise WsmanError("AMT WS-Man HTTP 400 from host: ...d:InvalidRepresentation...")
        return orig_put(uri, body, selectors=selectors)

    monkeypatch.setattr(drv._wsman, "put", fake_put)
    with pytest.raises(CapabilityError, match="boot-to-BIOS-setup"):
        drv.set_boot_device("bios")
    drv.set_boot_device("pxe")  # BIOSSetup=false path is unaffected — must not raise


def test_known_quirks_includes_bios_and_kvm(amt_emu):
    ids = {q.id for q in make(amt_emu).known_quirks()}
    assert "bios-boot-target-firmware-dependent" in ids
    assert "kvm-single-session" in ids


def test_set_boot_usb_rejected(amt_emu):
    with pytest.raises(KVMPilotError):
        make(amt_emu).set_boot_device("usb")


def test_set_boot_persistent_rejected(amt_emu):
    with pytest.raises(CapabilityError):
        make(amt_emu).set_boot_device("pxe", once=False)


def test_set_boot_dry_run_makes_no_writes(amt_emu):
    # Dry-run returns get_boot_options() (a read-back, like Redfish/IPMI) — reads
    # are fine; the invariant is that no state-changing call is sent.
    make(amt_emu, dry_run=True).set_boot_device("pxe")
    methods = [a for a, _ in amt_emu.state.calls]
    assert "ChangeBootOrder" not in methods
    assert "SetBootConfigRole" not in methods
    assert "Put" not in methods
    assert amt_emu.state.boot_order == ""  # unchanged


def test_get_boot_options_shape(amt_emu):
    opts = make(amt_emu).get_boot_options()
    assert opts["once"] is True
    assert opts["persistent"] is False
    assert "pxe" in opts["allowable"]
    assert "usb" not in opts["allowable"]


# -- boot read-back honesty (AMT's source override is write-only) ----------


def test_boot_readback_unknown_when_amt_omits_bootorder(amt_emu):
    # Real AMT: CIM_BootConfigSetting has no BootOrder -> we must NOT claim "none".
    amt_emu.state.boot_order_readable = False
    opts = make(amt_emu).get_boot_options()
    assert opts["enabled"] == "Unknown"
    assert opts["target"] is None
    assert opts["override_readable"] is False


def test_boot_readback_roundtrips_when_readable(amt_emu):
    # The emulator models BootOrder, so a set source reads back (readable=True).
    drv = make(amt_emu)
    drv.set_boot_device("pxe")
    opts = drv.get_boot_options()
    assert opts["target"] == "pxe"
    assert opts["enabled"] == "Once"
    assert opts["override_readable"] is True


def test_boot_bios_setup_is_readable(amt_emu):
    amt_emu.state.boot_order_readable = False  # even when the source override isn't
    make(amt_emu).set_boot_device("bios")
    opts = make(amt_emu).get_boot_options()
    assert opts["target"] == "bios"
    assert opts["enabled"] == "Once"
    assert opts["override_readable"] is True  # BIOSSetup IS readable


# -- feature enablement over WS-Man (SOL / KVM) ---------------------------


def test_enable_sol_opens_listener(amt_emu):
    make(amt_emu).enable_sol()
    assert amt_emu.state.redir_listener == "true"
    assert amt_emu.state.redir_state == "32771"  # IDER+SOL both


def test_enable_sol_gated(amt_emu):
    make(amt_emu, dry_run=True).enable_sol()
    assert amt_emu.state.redir_listener == "false"
    with pytest.raises(SafetyError):
        AmtDriver("127.0.0.1", "admin", "secret", port=amt_emu.port, confirm=deny_all).enable_sol()


def test_enable_kvm_with_consent(amt_emu):
    make(amt_emu, kvm_password="Abcd123!").enable_kvm()
    assert amt_emu.state.kvm_5900 == "true"
    assert amt_emu.state.kvm_rfb_password == "Abcd123!"
    assert amt_emu.state.kvm_sap_requested == "2"      # SAP enabled
    assert amt_emu.state.kvm_optin_policy == "true"    # consent kept
    assert amt_emu.state.optin_required == "1"          # global consent untouched


def test_enable_kvm_consent_off_in_acm(amt_emu):
    amt_emu.state.control_mode = "2"  # ACM
    make(amt_emu, kvm_password="Abcd123!").enable_kvm(require_consent=False)
    assert amt_emu.state.kvm_optin_policy == "false"
    assert amt_emu.state.optin_required == "0"           # global consent cleared


def test_enable_kvm_consent_off_rejected_in_ccm(amt_emu):
    amt_emu.state.control_mode = "1"  # CCM — consent is mandatory
    with pytest.raises(CapabilityError):
        make(amt_emu, kvm_password="Abcd123!").enable_kvm(require_consent=False)
    assert amt_emu.state.kvm_5900 == "false"  # nothing changed


def test_enable_kvm_rejects_bad_rfb_password(amt_emu):
    # default profile password "secret" is 6 chars / no complexity -> rejected early
    with pytest.raises(KVMPilotError):
        make(amt_emu).enable_kvm()
    assert amt_emu.state.kvm_5900 == "false"


def test_enable_kvm_gated(amt_emu):
    make(amt_emu, kvm_password="Abcd123!", dry_run=True).enable_kvm()
    assert amt_emu.state.kvm_5900 == "false"
    assert amt_emu.state.kvm_sap_requested is None


# -- auth -----------------------------------------------------------------


def test_digest_auth_roundtrip_succeeds(amt_emu):
    amt_emu.state.require_auth = True  # emulator challenges; driver completes the handshake
    assert make(amt_emu).is_powered_on() in (True, False)


def test_auth_rejection_raises_autherror(amt_emu):
    amt_emu.state.require_auth = True
    amt_emu.state.reject_auth = True
    with pytest.raises(AuthError):
        make(amt_emu).is_powered_on()


# -- SOL serial console (amtterm shell-out) -------------------------------


def test_serial_requires_amtterm(amt_emu, monkeypatch):
    from kvm_pilot.drivers.amt import driver as amt_mod

    monkeypatch.setattr(amt_mod.shutil, "which", lambda _n: None)
    with pytest.raises(CapabilityError):
        make(amt_emu).serial_interactive()


def test_serial_interactive_argv_and_env(amt_emu, monkeypatch):
    import types

    from kvm_pilot.drivers.amt import driver as amt_mod

    monkeypatch.setattr(amt_mod.shutil, "which", lambda _n: "/usr/bin/amtterm")
    seen: dict = {}

    def fake_run(argv, env=None, **kw):
        seen["argv"], seen["env"] = argv, env
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(amt_mod.subprocess, "run", fake_run)
    assert make(amt_emu).serial_interactive() == 0
    assert seen["argv"][0].endswith("amtterm")
    assert "16994" in seen["argv"]  # the SOL port, not the WS-Man port
    assert seen["env"]["AMT_PASSWORD"] == "secret"  # password via env…
    assert "secret" not in " ".join(seen["argv"])  # …never argv/ps


def test_serial_interactive_dry_run_does_not_run(amt_emu, monkeypatch):
    from kvm_pilot.drivers.amt import driver as amt_mod

    monkeypatch.setattr(amt_mod.shutil, "which", lambda _n: "/usr/bin/amtterm")
    monkeypatch.setattr(amt_mod.subprocess, "run", lambda *a, **k: pytest.fail("ran under dry-run"))
    drv = AmtDriver("127.0.0.1", "admin", "secret", port=amt_emu.port, dry_run=True)
    assert drv.serial_interactive() == 0


def test_serial_read_write_roundtrip(amt_emu, monkeypatch):
    import socket

    a, b = socket.socketpair()
    try:
        drv = make(amt_emu)
        # Stand in for a live SOL PTY with a socketpair: serial_write -> a -> b,
        # serial_read <- a <- b. Exercises the read/write drain logic directly.
        monkeypatch.setattr(drv, "_sol_activate", lambda: a.fileno())
        drv.serial_write("boot\r")
        assert b.recv(100) == b"boot\r"
        b.sendall(b"GRUB> ")
        assert "GRUB> " in drv.serial_read(timeout=1.0)
    finally:
        a.close()
        b.close()


# -- construction ---------------------------------------------------------


def test_from_config(amt_emu):
    from kvm_pilot.config import resolve_host

    cfg = resolve_host(
        host="127.0.0.1", driver="amt", user="admin", passwd="secret", amt_port=amt_emu.port
    )
    drv = AmtDriver.from_config(cfg)
    assert drv.host == "127.0.0.1"
    assert Capability.POWER in drv.capabilities()


def test_from_config_reads_kvm_password(amt_emu):
    from kvm_pilot.config import resolve_host

    cfg = resolve_host(
        host="127.0.0.1", driver="amt", user="admin", passwd="secret",
        amt_port=amt_emu.port, amt_kvm_password="rfb-only",
    )
    drv = AmtDriver.from_config(cfg)
    assert drv._kvm_password == "rfb-only"  # a *separate* MEBx credential from passwd


# -- RFB video snapshot + HID (KVM redirection) ---------------------------
#
# The firmware-level BIOS/POST/GRUB screenshot — the capability that is the
# whole reason AMT matters on a laptop the HDMI-capture KVM can't see boot on.
# Driven against a pure-stdlib RFB *server* emulator on loopback.


def make_rfb(amt_rfb, **kw) -> AmtDriver:
    return AmtDriver(
        "127.0.0.1", "admin", "secret",
        kvm_port=amt_rfb.port, kvm_password="rfbpass",
        confirm=lambda *_: True, **kw,
    )


def _wait_for(pred, timeout: float = 2.0) -> bool:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _png_dims(png: bytes) -> tuple[int, int]:
    import struct

    w, h = struct.unpack(">II", png[16:24])
    return w, h


def _png_first_pixel(png: bytes) -> tuple[int, int, int, int]:
    import struct
    import zlib

    i = png.index(b"IDAT")
    ln = struct.unpack(">I", png[i - 4:i])[0]
    raw = zlib.decompress(png[i + 4:i + 4 + ln])
    assert raw[0] == 0  # scanline filter: none
    return raw[1], raw[2], raw[3], raw[4]


def test_snapshot_captures_framebuffer(amt_rfb):
    png = make_rfb(amt_rfb).snapshot()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert _png_dims(png) == (amt_rfb.width, amt_rfb.height)
    # Emulator's top-left pixel is red -> proves RAW(BGRA) -> RGBA -> PNG end-to-end.
    assert _png_first_pixel(png) == (255, 0, 0, 255)


def test_snapshot_save_writes_png(amt_rfb, tmp_path):
    out = make_rfb(amt_rfb).snapshot_save(str(tmp_path / "bios.png"))
    assert out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_snapshot_base64_is_that_png(amt_rfb):
    import base64

    b64 = make_rfb(amt_rfb).snapshot_base64()
    assert base64.b64decode(b64)[:8] == b"\x89PNG\r\n\x1a\n"


def test_snapshot_auth_failure_raises(amt_rfb):
    amt_rfb.reject_auth = True  # server returns SecurityResult != 0
    with pytest.raises(AuthError):
        make_rfb(amt_rfb).snapshot()


def test_type_text_sends_key_events(amt_rfb):
    make_rfb(amt_rfb).type_text("hi")
    assert _wait_for(lambda: len(amt_rfb.keys) >= 4)
    # each char = a down then an up
    assert amt_rfb.keys == [(1, ord("h")), (0, ord("h")), (1, ord("i")), (0, ord("i"))]


def test_press_key_taps_named_key(amt_rfb):
    make_rfb(amt_rfb).press_key("Enter")
    assert _wait_for(lambda: len(amt_rfb.keys) >= 2)
    assert amt_rfb.keys == [(1, 0xFF0D), (0, 0xFF0D)]  # XK_Return down/up


def test_send_shortcut_is_a_chord(amt_rfb):
    make_rfb(amt_rfb).send_shortcut("Ctrl+Alt+Delete")
    assert _wait_for(lambda: len(amt_rfb.keys) >= 6)
    downs = [k for k in amt_rfb.keys if k[0] == 1]
    ups = [k for k in amt_rfb.keys if k[0] == 0]
    # all modifiers/keys press down, then release in reverse — a real chord.
    assert [s for _, s in downs] == [0xFFE3, 0xFFE9, 0xFFFF]
    assert [s for _, s in ups] == [0xFFFF, 0xFFE9, 0xFFE3]


def test_mouse_move_then_click(amt_rfb):
    drv = make_rfb(amt_rfb)
    drv.mouse_move(10, 20)
    drv.mouse_click("left")
    assert _wait_for(lambda: len(amt_rfb.pointers) >= 3)
    # move (button mask 0), then a left click: press (mask 1) + release (mask 0)
    assert amt_rfb.pointers == [(0, 10, 20), (1, 10, 20), (0, 10, 20)]


def test_hid_rich_cli_signatures(amt_rfb):
    # The CLI/MCP call the richer KVMClient signatures; AMT must accept them
    # rather than TypeError (the P0 bug where `type`/`click`/`mouse-move` crashed).
    drv = make_rfb(amt_rfb)
    drv.type_text("hi", slow=True, delay=0.0)           # slow=/delay= must not raise
    assert _wait_for(lambda: len(amt_rfb.keys) >= 4)
    drv.mouse_click("left", hold_ms=10, double=True)    # hold_ms=/double= must not raise
    assert _wait_for(lambda: len(amt_rfb.pointers) >= 4)  # double = two down/up pairs


def test_mouse_move_percent_maps_onto_framebuffer(amt_rfb):
    # 2x2 emulator framebuffer -> percent maps onto real pixels (0..w-1).
    drv = make_rfb(amt_rfb)
    drv.mouse_move_percent(1.0, 1.0)
    drv.mouse_move_pixels(0, 0)
    assert _wait_for(lambda: len(amt_rfb.pointers) >= 2)
    assert amt_rfb.pointers[0] == (0, 1, 1)   # 100% of a 2px axis -> pixel 1
    assert amt_rfb.pointers[1] == (0, 0, 0)   # pixel-native passthrough


def test_hid_dry_run_sends_nothing(amt_rfb):
    make_rfb(amt_rfb, dry_run=True).press_key("Enter")
    assert not _wait_for(lambda: len(amt_rfb.keys) >= 1, timeout=0.3)


def test_hid_deny_sends_nothing(amt_rfb):
    drv = AmtDriver("127.0.0.1", "admin", "secret", kvm_port=amt_rfb.port, confirm=deny_all)
    with pytest.raises(SafetyError):
        drv.press_key("Enter")
    assert not _wait_for(lambda: len(amt_rfb.keys) >= 1, timeout=0.3)


# -- RFB primitives (no server needed) ------------------------------------


def test_des_matches_fips_46_3_vector():
    # FIPS 46-3 single-block known-answer: proves the inline DES (VNC auth relies on it).
    from kvm_pilot.drivers.amt.rfb import des_encrypt_block

    ct = des_encrypt_block(bytes.fromhex("0123456789ABCDEF"), bytes.fromhex("4E6F772069732074"))
    assert ct.hex().upper() == "3FA40E8A984D4815"


def test_vnc_auth_response_is_16_bytes():
    from kvm_pilot.drivers.amt.rfb import vnc_auth_response

    assert len(vnc_auth_response("rfbpass", b"\x00" * 16)) == 16


def test_encode_png_roundtrips_pixels():
    from kvm_pilot.drivers.amt.rfb import encode_png

    # 2x1: red then green, RGBA in / RGBA scanline out.
    png = encode_png(2, 1, b"\xff\x00\x00\xff\x00\xff\x00\xff")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert _png_dims(png) == (2, 1)
    assert _png_first_pixel(png) == (255, 0, 0, 255)


def test_key_to_keysym_named_and_literal():
    from kvm_pilot.drivers.amt.rfb import key_to_keysym

    assert key_to_keysym("Enter") == 0xFF0D
    assert key_to_keysym("F2") == 0xFFBF
    assert key_to_keysym("a") == ord("a")


# -- ZRLE tile decode (AMT RLE(16), the hybrid-GPU path) -------------------
# Hand-crafted vectors for each sub-encoding — independent of any encoder, so a
# bug in one can't mask a bug in the other. Colours in RGB565: red=0xF800,
# green=0x07E0, blue=0x001F, white=0xFFFF, black=0x0000.


def _zrle(u):
    from kvm_pilot.drivers.amt.rfb import _decode_zrle_tile

    return _decode_zrle_tile(bytes(u), 2, 2)


def test_zrle_solid():
    assert _zrle([1, 0x00, 0xF8]) == [0xF800] * 4  # solid red


def test_zrle_raw():
    assert _zrle([0, 0x00, 0xF8, 0xE0, 0x07, 0x1F, 0x00, 0xFF, 0xFF]) == \
        [0xF800, 0x07E0, 0x001F, 0xFFFF]


def test_zrle_packed_palette():
    # 2-colour [black, white], 1 bpp, byte-aligned rows -> a checkerboard
    assert _zrle([2, 0x00, 0x00, 0xFF, 0xFF, 0x80, 0x40]) == [0xFFFF, 0x0000, 0x0000, 0xFFFF]


def test_zrle_plain_rle():
    # red run 3 (len byte 2 -> 1+2), blue run 1 (len byte 0 -> 1+0)
    assert _zrle([128, 0x00, 0xF8, 2, 0x1F, 0x00, 0]) == [0xF800, 0xF800, 0xF800, 0x001F]


def test_zrle_palette_rle():
    # palette [red, blue]; index 0 with run (0x80 + len 2), then index 1 (run 1)
    assert _zrle([130, 0x00, 0xF8, 0x1F, 0x00, 0x80, 2, 1]) == [0xF800, 0xF800, 0xF800, 0x001F]


# -- healthcheck (AMT posture + quirks) -----------------------------------


def _health(amt_emu):
    from kvm_pilot.health import run_healthcheck

    return {r.id: r for r in run_healthcheck(make(amt_emu)).results}


def test_healthcheck_flags_plaintext_transport(amt_emu):
    from kvm_pilot.health import Severity

    assert _health(amt_emu)["amt-transport"].severity is Severity.WARNING  # amt_tls default False


def test_healthcheck_critical_when_unprovisioned(amt_emu):
    from kvm_pilot.health import Severity

    amt_emu.state.provisioning_state = "0"  # pre-provisioning
    assert _health(amt_emu)["amt-provisioning"].severity is Severity.CRITICAL


def test_healthcheck_flags_kvm_consent_off(amt_emu):
    from kvm_pilot.health import Severity

    amt_emu.state.kvm_5900 = "true"
    amt_emu.state.kvm_optin_policy = "false"
    amt_emu.state.optin_required = "0"
    assert _health(amt_emu)["amt-kvm-consent"].severity is Severity.WARNING


def test_healthcheck_redirection_ok_when_listeners_on(amt_emu):
    from kvm_pilot.health import Severity

    amt_emu.state.redir_listener = "true"
    amt_emu.state.kvm_5900 = "true"
    assert _health(amt_emu)["amt-redirection"].severity is Severity.OK


def test_healthcheck_reports_amt_quirks(amt_emu):
    detail = _health(amt_emu)["firmware-quirks"].detail
    assert "kvm-single-session" in detail
    assert "kvm-graphical-only" in detail


def test_amt_health_is_memoized(amt_emu):
    # The AMT checks share ONE amt_health() read set — AMT flood-protects bursts.
    drv = make(amt_emu)
    drv.amt_health()
    before = len(amt_emu.state.calls)
    drv.amt_health()  # cached: no new WS-Man calls
    assert len(amt_emu.state.calls) == before


def test_healthcheck_skips_amt_checks_on_non_amt_driver():
    # amt_health-guarded checks must self-skip on other drivers (fake has no amt_health).
    from kvm_pilot.drivers.fake import FakeDriver
    from kvm_pilot.health import run_healthcheck

    ids = {r.id for r in run_healthcheck(FakeDriver()).results}
    assert not any(i.startswith("amt-") for i in ids)


# -- WS-Man transport-fault taxonomy --------------------------------------
# Each transport failure must map onto the right kvm-pilot error type, and the
# password must never leak into a raised message. is_powered_on() is the probe
# (its first WS-Man POST is the Enumerate that trips each knob).


def test_http_500_becomes_wsman_error(amt_emu):
    amt_emu.state.http_status = 500
    with pytest.raises(WsmanError):
        make(amt_emu).is_powered_on()


def test_read_timeout_becomes_timeout_error(amt_emu):
    # The server stalls past the client deadline -> a WS-Man timeout, not a hang.
    amt_emu.state.delay = 0.6
    drv = AmtDriver("127.0.0.1", "admin", "secret", port=amt_emu.port, timeout=0.2, confirm=lambda *_: True)
    with pytest.raises(KpTimeoutError):
        drv.is_powered_on()


def test_connection_refused_becomes_connection_error():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    drv = AmtDriver("127.0.0.1", "admin", "secret", port=port, timeout=1.0, confirm=lambda *_: True)
    with pytest.raises(ConnectionError):
        drv.is_powered_on()


def test_non_xml_body_becomes_protocol_error(amt_emu):
    amt_emu.state.garbage_body = True
    with pytest.raises(ProtocolError):
        make(amt_emu).is_powered_on()


def test_error_body_redacts_the_password(amt_emu):
    # A fault body that echoes the admin password must never surface it verbatim.
    amt_emu.state.http_status = 500
    amt_emu.state.error_body = "<fault>admin password secret leaked here</fault>"
    with pytest.raises(WsmanError) as ei:
        make(amt_emu).is_powered_on()
    msg = str(ei.value)
    assert "secret" not in msg          # the raw credential is gone…
    assert "REDACTED" in msg            # …replaced by the redaction marker


# -- WS-Man device-refused + enumeration behaviour ------------------------


def test_power_nonzero_return_raises(amt_emu):
    # HTTP-200 but ReturnValue != 0 = the ME accepted the SOAP and refused the op.
    amt_emu.state.nonzero_methods = {"RequestPowerStateChange"}
    with pytest.raises(WsmanError):
        make(amt_emu).power_on()


def test_change_boot_order_nonzero_return_raises(amt_emu):
    amt_emu.state.nonzero_methods = {"ChangeBootOrder"}
    with pytest.raises(WsmanError):
        make(amt_emu).set_boot_device("pxe")


def test_set_boot_config_role_nonzero_return_raises(amt_emu):
    # ChangeBootOrder succeeds; the single-use SetBootConfigRole is what's refused.
    amt_emu.state.nonzero_methods = {"SetBootConfigRole"}
    with pytest.raises(WsmanError):
        make(amt_emu).set_boot_device("pxe")


def test_kvm_sap_nonzero_return_raises(amt_emu):
    amt_emu.state.nonzero_methods = {"RequestStateChange"}
    with pytest.raises(WsmanError):
        make(amt_emu, kvm_password="Abcd123!").enable_kvm()


def test_enumerate_follows_pull_pagination(amt_emu):
    # AMT returns a continuation context before EndOfSequence; the driver pulls twice.
    amt_emu.state.enum_pages = True
    assert make(amt_emu).is_powered_on() in (True, False)
    assert amt_emu.state.pull_count == 2  # first page + the continuation page


def test_is_powered_on_raises_without_powerstate(amt_emu):
    # The association instance exists but omits PowerState -> we must not guess.
    amt_emu.state.power_state_missing = True
    with pytest.raises(WsmanError):
        make(amt_emu).is_powered_on()


def test_is_powered_on_raises_on_empty_enumeration(amt_emu):
    # An Enumerate with no context yields no instances -> no PowerState to read.
    amt_emu.state.enum_no_context = True
    with pytest.raises(WsmanError):
        make(amt_emu).is_powered_on()


def test_amt_version_falls_back_to_software_identity(amt_emu):
    # When AMT_SetupAndConfigurationService omits the version, the driver recovers
    # it from a CIM_SoftwareIdentity whose InstanceID marks it as the AMT firmware.
    amt_emu.state.amt_setup_no_version = True
    assert make(amt_emu).get_firmware_info()["version"] == "16.1.25"


def test_amt_version_none_when_nothing_reports_it(amt_emu):
    amt_emu.state.amt_setup_no_version = True
    amt_emu.state.swid_no_amt = True  # and no AMT-shaped SoftwareIdentity to fall back on
    assert make(amt_emu).get_firmware_info()["version"] is None


def test_get_info_tolerates_missing_chassis_and_uuid(amt_emu):
    # Best-effort identity: a firmware that omits these classes blanks only them.
    amt_emu.state.suppress_classes = {"CIM_Chassis", "CIM_ComputerSystemPackage"}
    info = make(amt_emu).get_info()
    assert info["manufacturer"] is None
    assert info["uuid"] is None
    assert info["amt_version"] == "16.1.25"  # unrelated lookups still succeed


# -- amt_health resilience ------------------------------------------------


def test_amt_health_tolerates_total_failure(amt_emu):
    # Every WS-Man read faults -> amt_health still returns a full dict (all-None
    # posture), never raising into the healthcheck.
    amt_emu.state.fault_reason = "ME busy"
    h = make(amt_emu).amt_health()
    assert h["sol_listener"] is None
    assert h["kvm_5900"] is None
    assert h["control_mode"] is None          # IPS_HostBasedSetupService unreadable
    assert h["kvm_consent_required"] is None  # neither KVM settings nor OptIn readable


def test_amt_health_reports_valid_rfb_password(amt_emu):
    # A compliant 8-char RFB credential reads back as OK in the posture.
    h = make(amt_emu, kvm_password="Abcd123!").amt_health()
    assert h["rfb_password_ok"] is True


# -- SOL activation over a PTY (amtterm child) ----------------------------
# Mirrors the IPMI SOL-over-PTY test: fake the PTY + amtterm child so the real
# _sol_activate path is exercised without spawning anything.


def _fake_sol(monkeypatch, master: int = 7, slave: int = 8):
    import types

    from kvm_pilot.drivers.amt import driver as amt_mod

    rec = types.SimpleNamespace(argv=None, env=None, popens=0, set_blocking=[], closed=[])

    class FakeProc:
        def __init__(self, argv, **kw):
            rec.argv, rec.env, rec.popens = argv, kw.get("env"), rec.popens + 1

        def poll(self):
            return None  # a live child

    import pty  # the driver imports this lazily; patch the shared module object

    monkeypatch.setattr(amt_mod.shutil, "which", lambda _n: "/usr/bin/amtterm")
    monkeypatch.setattr(amt_mod.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(pty, "openpty", lambda: (master, slave))
    monkeypatch.setattr(amt_mod.os, "close", lambda fd: rec.closed.append(fd))
    monkeypatch.setattr(amt_mod.os, "set_blocking", lambda fd, b: rec.set_blocking.append((fd, b)))
    return rec


def test_sol_activate_spawns_amtterm_over_pty(amt_emu, monkeypatch):
    rec = _fake_sol(monkeypatch)
    drv = make(amt_emu)
    fd = drv._sol_activate()
    assert fd == 7
    assert rec.argv == ["amtterm", "127.0.0.1", "16994"]   # host + SOL port, not the WS-Man port
    assert rec.env["AMT_PASSWORD"] == "secret"             # password via env…
    assert "secret" not in rec.argv                        # …never argv/ps
    assert (7, False) in rec.set_blocking                  # master set non-blocking
    assert 8 in rec.closed                                 # slave closed in the parent
    # A live child (poll()->None) is reused: no second Popen, same fd.
    assert drv._sol_activate() == 7
    assert rec.popens == 1


def test_serial_read_requires_amtterm(amt_emu, monkeypatch):
    # The read/write pair activates lazily; a missing amtterm fails clearly there too
    # (not only on the interactive console path).
    from kvm_pilot.drivers.amt import driver as amt_mod

    monkeypatch.setattr(amt_mod.shutil, "which", lambda _n: None)
    with pytest.raises(CapabilityError):
        make(amt_emu).serial_read()


def test_serial_read_and_write_are_noops_under_dry_run(amt_emu, monkeypatch):
    from kvm_pilot.drivers.amt import driver as amt_mod

    monkeypatch.setattr(amt_mod.shutil, "which", lambda _n: "/usr/bin/amtterm")
    drv = AmtDriver("127.0.0.1", "admin", "secret", port=amt_emu.port, dry_run=True)
    assert drv.serial_read(timeout=0.1) == ""  # gate skips activation -> fd None
    drv.serial_write("x")                        # no fd -> quietly does nothing


def test_serial_read_breaks_on_pty_eio(amt_emu, monkeypatch):
    import socket

    from kvm_pilot.drivers.amt import driver as amt_mod

    a, b = socket.socketpair()
    try:
        drv = make(amt_emu)
        monkeypatch.setattr(drv, "_sol_activate", lambda: a.fileno())
        b.sendall(b"x")  # make the fd readable so select() returns it
        monkeypatch.setattr(
            amt_mod.os, "read", lambda *_a: (_ for _ in ()).throw(OSError("EIO"))
        )
        assert drv.serial_read(timeout=0.5) == ""  # EIO on the dead PTY -> empty, not a crash
    finally:
        a.close()
        b.close()


def test_serial_read_stops_on_peer_close(amt_emu, monkeypatch):
    import socket

    a, b = socket.socketpair()
    try:
        drv = make(amt_emu)
        monkeypatch.setattr(drv, "_sol_activate", lambda: a.fileno())
        b.close()  # peer gone -> fd readable, os.read returns b"" (EOF)
        assert drv.serial_read(timeout=0.5) == ""
    finally:
        a.close()


# -- SOL / RFB teardown (serial_close + close) ----------------------------


class _FakeProc:
    def __init__(self, wait_raises: bool = False):
        self.terminated = self.killed = self.waited = False
        self._wait_raises = wait_raises

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True
        if self._wait_raises:
            raise subprocess_TimeoutExpired()

    def kill(self):
        self.killed = True


def subprocess_TimeoutExpired():  # small factory so the class body stays import-light
    import subprocess

    return subprocess.TimeoutExpired(cmd="amtterm", timeout=5)


def test_serial_close_terminates_child_and_frees_fd(amt_emu):
    import os

    drv = make(amt_emu)
    proc = _FakeProc()
    r, w = os.pipe()  # a real fd so os.close(fd) actually runs
    drv._sol, drv._sol_fd = proc, r
    drv.serial_close()
    assert proc.terminated and proc.waited and not proc.killed
    assert drv._sol is None and drv._sol_fd is None
    os.close(w)  # r is already closed by serial_close


def test_serial_close_kills_when_wait_times_out(amt_emu):
    drv = make(amt_emu)
    proc = _FakeProc(wait_raises=True)
    drv._sol, drv._sol_fd = proc, 10_000_007  # a bogus fd -> os.close swallows EBADF
    drv.serial_close()
    assert proc.terminated and proc.killed  # terminate then, on the wait timeout, kill


def test_close_tears_down_sol_and_hid(amt_emu):
    drv = make(amt_emu)
    proc = _FakeProc()
    drv._sol, drv._sol_fd = proc, None

    class _Hid:
        closed = False

        def close(self):
            self.closed = True

    hid = _Hid()
    drv._hid = hid
    drv.close()
    assert proc.terminated             # SOL child stopped
    assert hid.closed                  # RFB HID session closed
    assert drv._hid is None


def test_close_with_no_hid_is_safe(amt_emu):
    # close() with only a SOL child (no RFB HID session) tears down and returns.
    drv = make(amt_emu)
    proc = _FakeProc()
    drv._sol, drv._sol_fd = proc, None
    drv.close()
    assert proc.terminated and drv._hid is None


def test_close_swallows_hid_close_error(amt_emu):
    drv = make(amt_emu)

    class _BadHid:
        def close(self):
            raise RuntimeError("already gone")

    drv._hid = _BadHid()
    drv.close()  # must not raise despite the HID teardown throwing
    assert drv._hid is None


# -- reset_kvm_session + snapshot single-session retry --------------------


def test_reset_kvm_session_swallows_wsman_errors(amt_emu, monkeypatch):
    from kvm_pilot.drivers.amt import driver as amt_mod

    monkeypatch.setattr(amt_mod.time, "sleep", lambda *_a: None)  # no real 5s wait
    amt_emu.state.fault_reason = "SAP busy"
    make(amt_emu).reset_kvm_session()  # a WS-Man fault mid-cycle is best-effort -> no raise


def _driver_wsman_and_rfb(amt_emu, amt_rfb, **kw) -> AmtDriver:
    return AmtDriver(
        "127.0.0.1", "admin", "secret",
        port=amt_emu.port, kvm_port=amt_rfb.port, kvm_password="rfbpass",
        confirm=lambda *_: True, **kw,
    )


def test_snapshot_cycles_sap_and_retries_after_a_dropped_session(amt_emu, amt_rfb, monkeypatch):
    from kvm_pilot.drivers.amt import driver as amt_mod

    monkeypatch.setattr(amt_mod.time, "sleep", lambda *_a: None)  # skip reset_kvm_session's waits
    amt_rfb.drop_first = 1  # the first KVM connection is wedged/dropped
    png = _driver_wsman_and_rfb(amt_emu, amt_rfb).snapshot()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"                 # the retry succeeded
    assert amt_emu.state.kvm_sap_requests == ["3", "2"]   # SAP cycled disable(3)->enable(2)


def test_snapshot_reraises_when_every_attempt_drops(amt_emu, amt_rfb, monkeypatch):
    from kvm_pilot.drivers.amt import driver as amt_mod

    monkeypatch.setattr(amt_mod.time, "sleep", lambda *_a: None)
    amt_rfb.drop_first = 5  # more than the 3 attempts -> the ConnectionError survives
    with pytest.raises(ConnectionError):
        _driver_wsman_and_rfb(amt_emu, amt_rfb).snapshot()


# -- RFB handshake + transport error mapping ------------------------------


def _rfb(port: int, password: str = "rfbpass"):
    from kvm_pilot.drivers.amt.rfb import Rfb

    return Rfb("127.0.0.1", port, password, timeout=5.0)


def test_handshake_rejects_non_rfb_server(amt_rfb):
    amt_rfb.bad_protocol = True
    with pytest.raises(ProtocolError):
        _rfb(amt_rfb.port).connect()


def test_handshake_surfaces_reason_string_then_drop(amt_rfb):
    amt_rfb.reason_drop = True
    with pytest.raises(AuthError):
        _rfb(amt_rfb.port).connect()


def test_handshake_rejects_when_no_vnc_auth_offered(amt_rfb):
    amt_rfb.no_vnc_auth = True
    with pytest.raises(AuthError):
        _rfb(amt_rfb.port).connect()


def test_handshake_rejects_non_16bpp_framebuffer(amt_rfb):
    amt_rfb.bad_bpp = True
    with pytest.raises(ProtocolError):
        _rfb(amt_rfb.port).connect()


def test_connect_to_closed_port_raises_connection_error():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(ConnectionError):
        _rfb(port).connect()


def test_recv_maps_peer_close_to_connection_error(amt_rfb):
    amt_rfb.drop_first = 1  # closed before the RFB banner -> _recv sees EOF
    with pytest.raises(ConnectionError):
        _rfb(amt_rfb.port).connect()


def test_recv_maps_socket_error_to_connection_error(amt_rfb):
    r = _rfb(amt_rfb.port)
    r.connect()
    r._sock.setblocking(False)  # no pending data -> recv raises BlockingIOError (OSError)
    with pytest.raises(ConnectionError):
        r._recv(1)


def test_send_on_dropped_socket_raises_connection_error(amt_rfb):
    r = _rfb(amt_rfb.port)
    r.connect()
    r._sock.close()  # AMT dropped us; the socket object is still set
    with pytest.raises(ConnectionError):
        r.key(0xFF0D, True)  # _send -> sendall on a closed socket


def test_close_is_safe_without_connect():
    _rfb(1).close()  # _sock is None -> a no-op teardown


# -- RFB multi-frame / RLE / DesktopSize / control-message plumbing -------


def test_snapshot_assembles_tiles_across_updates(amt_rfb):
    # AMT sends the screen as many small tiles spread over several update messages.
    amt_rfb.tile_mode = True
    png = make_rfb(amt_rfb).snapshot()
    assert _png_dims(png) == (amt_rfb.width, amt_rfb.height)
    assert _png_first_pixel(png) == (255, 0, 0, 255)  # tile (0,0) is still red


def test_snapshot_decodes_an_rle_rect_end_to_end(amt_rfb):
    # A single RLE(16) rect through a real standard-zlib stream must round-trip.
    amt_rfb.rle_mode = True
    png = make_rfb(amt_rfb).snapshot()
    assert _png_dims(png) == (amt_rfb.width, amt_rfb.height)
    assert _png_first_pixel(png) == (255, 0, 0, 255)


def test_snapshot_restarts_on_desktop_resize(amt_rfb):
    # A DesktopSize(-223) pseudo-rect forces the client to restart the capture at
    # the new geometry rather than mis-assembling the old one.
    amt_rfb.resize_first_to = (3, 2)
    png = make_rfb(amt_rfb).snapshot()
    assert _png_dims(png) == (3, 2)
    assert _png_first_pixel(png) == (255, 0, 0, 255)  # solid (resized) frame


def test_snapshot_ignores_bell_and_cuttext(amt_rfb):
    # Bell(2) / ServerCutText(3) interleaved with updates must be skipped, not fatal.
    amt_rfb.inject_control = True
    png = make_rfb(amt_rfb).snapshot()
    assert _png_dims(png) == (amt_rfb.width, amt_rfb.height)
    assert _png_first_pixel(png) == (255, 0, 0, 255)


def test_snapshot_rejects_unsupported_rect_encoding(amt_rfb):
    amt_rfb.bad_encoding = True
    with pytest.raises(ProtocolError):
        make_rfb(amt_rfb).snapshot()


def test_snapshot_rejects_unexpected_server_message(amt_rfb):
    amt_rfb.bad_message = True
    with pytest.raises(ProtocolError):
        make_rfb(amt_rfb).snapshot()


def test_snapshot_gives_up_if_size_keeps_changing(amt_rfb):
    amt_rfb.always_resize = True  # every update is a DesktopSize -> never converges
    with pytest.raises(ProtocolError):
        make_rfb(amt_rfb).snapshot()


def test_snapshot_rejects_zero_sized_framebuffer():
    from amt_rfb_emulator import AmtRfbEmulator

    with AmtRfbEmulator(width=0, height=2, pixels=[]) as e:
        drv = AmtDriver(
            "127.0.0.1", "admin", "secret",
            kvm_port=e.port, kvm_password="rfbpass", confirm=lambda *_: True,
        )
        with pytest.raises(ProtocolError):
            drv.snapshot()


# -- ZRLE tile edge cases + web-code keysyms ------------------------------


def test_zrle_unknown_subencoding_raises():
    from kvm_pilot.drivers.amt.rfb import _decode_zrle_tile

    with pytest.raises(ProtocolError):
        _decode_zrle_tile(bytes([17]), 2, 2)   # 17: neither packed-palette (<=16) nor RLE
    with pytest.raises(ProtocolError):
        _decode_zrle_tile(bytes([129]), 2, 2)  # 129: not plain-RLE(128), not palette-RLE(>=130)


def test_zrle_plain_rle_run_exceeds_255():
    # A run longer than 255 continues with 0xFF bytes: run = 1 + 255 + 1 = 257.
    assert _zrle([128, 0x00, 0xF8, 255, 1]) == [0xF800] * 4  # solid red, truncated to the tile


def test_zrle_palette_rle_run_exceeds_255():
    # Palette-RLE with a >255 run: index 0 (high bit set) then 0xFF-continuation.
    assert _zrle([130, 0x00, 0xF8, 0x1F, 0x00, 0x80, 255, 1]) == [0xF800] * 4


def test_key_to_keysym_web_codes_and_unknown():
    from kvm_pilot.drivers.amt.rfb import key_to_keysym

    assert key_to_keysym("KeyA") == ord("a")     # KeyA..KeyZ -> the literal letter
    assert key_to_keysym("Digit1") == ord("1")   # Digit0..Digit9 -> the literal digit
    with pytest.raises(KVMPilotError):
        key_to_keysym("NoSuchKey")


# -- HID gating on the remaining verbs ------------------------------------


def test_type_text_slow_paces_keystrokes(amt_rfb):
    # slow=True with a real delay walks the inter-keystroke sleep path.
    make_rfb(amt_rfb).type_text("hi", slow=True, delay=0.001)
    assert _wait_for(lambda: len(amt_rfb.keys) >= 4)


def test_type_text_dry_run_sends_nothing(amt_rfb):
    make_rfb(amt_rfb, dry_run=True).type_text("hi")
    assert not _wait_for(lambda: len(amt_rfb.keys) >= 1, timeout=0.3)


def test_send_shortcut_dry_run_sends_nothing(amt_rfb):
    make_rfb(amt_rfb, dry_run=True).send_shortcut("Ctrl+Alt+Delete")
    assert not _wait_for(lambda: len(amt_rfb.keys) >= 1, timeout=0.3)


def test_mouse_click_dry_run_sends_no_button(amt_rfb):
    drv = make_rfb(amt_rfb, dry_run=True)
    drv.mouse_move(1, 1)     # moves are ungated -> the pointer still lands
    drv.mouse_click("left")  # the click is gated -> skipped under dry-run
    assert _wait_for(lambda: len(amt_rfb.pointers) >= 1)
    assert all(mask == 0 for mask, _, _ in amt_rfb.pointers)  # no press/release mask


# -- WS-Man helper functions (unit) ---------------------------------------


def test_wsman_tls_builds_https_opener():
    # Constructing with tls=True wires the HTTPS handler and an https:// URL
    # (no connection is opened here).
    from kvm_pilot.drivers.amt.wsman import Wsman

    w = Wsman("127.0.0.1", "admin", "secret", port=16993, tls=True)
    assert w._url.startswith("https://")


def test_fault_detail_prefers_subcode_and_ignores_nonfault():
    from xml.etree import ElementTree as ET

    from kvm_pilot.drivers.amt.wsman import _S, Wsman

    w = Wsman("127.0.0.1", "admin", "secret")  # builds the opener; opens no socket
    # No fault -> None, so the HTTP-error path falls back to raw text.
    assert w._fault_detail(ET.fromstring(f'<s:Envelope xmlns:s="{_S}"><s:Body/></s:Envelope>')) is None
    # A fault -> Reason + the specific Code/Subcode/Value, not the top-level class.
    fault = ET.fromstring(
        f'<s:Envelope xmlns:s="{_S}"><s:Body><s:Fault>'
        "<s:Code><s:Value>s:Receiver</s:Value>"
        "<s:Subcode><s:Value>e:TimedOut</s:Value></s:Subcode></s:Code>"
        "<s:Reason><s:Text>The operation has timed out.</s:Text></s:Reason>"
        "</s:Fault></s:Body></s:Envelope>"
    )
    assert w._fault_detail(fault) == "The operation has timed out. (subcode e:TimedOut)"


def test_wsman_small_helpers():
    from xml.etree import ElementTree as ET

    from kvm_pilot.drivers.amt import wsman as W

    # _body_child: no <Body> yields the root; an empty <Body> yields the Body itself.
    assert W._body_child(ET.fromstring("<Envelope/>")).tag == "Envelope"
    empty = ET.fromstring(f'<s:Envelope xmlns:s="{W._S}"><s:Body/></s:Envelope>')
    assert W._local(W._body_child(empty).tag) == "Body"
    el = ET.fromstring("<a xmlns='n'><b/><b/></a>")
    assert len(W.findall_local(el, "b")) == 2
