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
from kvm_pilot.errors import AuthError, CapabilityError, KVMPilotError, SafetyError
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


# -- system info ----------------------------------------------------------


def test_get_info_identity(amt_emu):
    info = make(amt_emu).get_info()
    assert info["manufacturer"] == "Dell Inc."
    assert info["model"] == "Latitude 5411"
    assert info["serial_number"] == "JXXD6D3"
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
