"""IpmiDriver tests (#62) — hardware-free.

Exercises the ipmitool wrapper end-to-end with a fake ``subprocess`` (no real
ipmitool / BMC): command construction, output parsing against captured
``ipmitool`` output shapes, capability detection, and safety gating.
"""

from __future__ import annotations

import types

import pytest

from kvm_pilot.drivers import ipmi as ipmi_mod
from kvm_pilot.drivers import make_driver, make_driver_from_config
from kvm_pilot.drivers.base import (
    BootConfig,
    Capability,
    Logs,
    Power,
    Sensors,
    SerialConsole,
    SystemInfo,
)
from kvm_pilot.drivers.ipmi import IpmiDriver
from kvm_pilot.errors import CapabilityError, KVMPilotError, SafetyError
from kvm_pilot.safety import deny_all

# --- captured ipmitool output shapes ---------------------------------------
POWER_ON = "Chassis Power is on\n"
POWER_OFF = "Chassis Power is off\n"
POWER_CONTROL = "Chassis Power Control: Up/On\n"
CHASSIS_STATUS = (
    "System Power         : on\n"
    "Power Overload       : false\n"
    "Last Power Event     : command\n"
)
# Real Dell iDRAC6 (R710) FRU shape: the server model is in *Board Product*, while
# *Product Name* is the iDRAC's configured hostname ("localhost") — a placeholder we
# must skip, not report as the model (#62 real-hardware finding).
FRU = (
    "FRU Device Description : Builtin FRU Device (ID 0)\n"
    " Board Mfg             : DELL\n"
    " Board Product         : PowerEdge R710\n"
    " Board Serial          : CN1374001D00VJ\n"
    " Product Manufacturer  : DELL\n"
    " Product Name          : localhost\n"
    " Product Serial        : ABC12345\n"
)
MC_INFO = (
    "Device ID                 : 32\n"
    "Firmware Revision         : 1.85\n"
    "Manufacturer Name         : DELL\n"
)
BOOT_PXE_ONCE_EFI = (
    "Boot parameter 5 is valid/unlocked\n"
    " Boot Flags :\n"
    "   - Boot Flag Valid\n"
    "   - Options apply to only next boot\n"
    "   - BIOS EFI boot\n"
    "   - Boot Device Selector : Force Boot from Network - PXE\n"
)
BOOT_DISK_PERSIST_LEGACY = (
    " Boot Flags :\n"
    "   - Options apply to all future boots\n"
    "   - BIOS PC Compatible (legacy) boot\n"
    "   - Boot Device Selector : Force Boot from default Hard-Drive\n"
)
SDR = (
    "CPU1 Temp        | 04h | ok  | 3.1 | 45 degrees C\n"
    "FAN1 RPM         | 30h | ok  | 7.1 | 4200 RPM\n"
    "Voltage 12V      | 60h | ok  | 7.2 | 12.10 Volts\n"
)
SEL = (
    "   1 | 07/13/2026 | 10:00:00 | Power Unit #0x01 | Power off/down | Asserted\n"
    "   2 | 07/13/2026 | 10:05:00 | System Boot Initiated | Initiated by power up\n"
)

# ordered (needle-in-joined-argv, output); first match wins — specific first.
_OUTPUTS = [
    ("chassis power status", POWER_ON),
    ("chassis power", POWER_CONTROL),      # on/off/soft/reset/cycle
    ("chassis bootparam get 5", BOOT_PXE_ONCE_EFI),
    ("chassis bootdev", "Set Boot Device to pxe\n"),
    ("chassis status", CHASSIS_STATUS),
    ("fru print", FRU),
    ("mc info", MC_INFO),
    ("sdr elist", SDR),
    ("sel list", SEL),
]


@pytest.fixture()
def ipmi(monkeypatch):
    """An IpmiDriver whose ipmitool is faked; returns (driver, calls)."""
    calls: list[list[str]] = []
    outputs = list(_OUTPUTS)

    def fake_run(argv, **kw):
        calls.append(argv)
        joined = " ".join(argv)
        for needle, out in outputs:
            if needle in joined:
                return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="unknown command")

    monkeypatch.setattr(ipmi_mod.shutil, "which", lambda _n: "/usr/bin/ipmitool")
    monkeypatch.setattr(ipmi_mod.subprocess, "run", fake_run)
    drv = IpmiDriver("10.0.1.99", "root", "calvin", confirm=lambda *a: True)
    drv._outputs = outputs  # let tests tweak responses
    return drv, calls


class TestCapabilities:
    def test_ipmi_capability_set(self, ipmi):
        drv, _ = ipmi
        caps = drv.capabilities()
        assert caps == {
            Capability.SYSTEM_INFO, Capability.POWER, Capability.BOOT_CONFIG,
            Capability.SENSORS, Capability.LOGS, Capability.SERIAL_CONSOLE,
        }
        for absent in (Capability.HID, Capability.VIDEO, Capability.VIRTUAL_MEDIA,
                       Capability.GPIO, Capability.EVENTS):
            assert absent not in caps

    def test_satisfies_protocols(self, ipmi):
        drv, _ = ipmi
        assert isinstance(drv, Power | SystemInfo | Sensors | Logs | BootConfig | SerialConsole)


class TestCommandConstruction:
    def test_base_argv_uses_env_password_not_argv(self, ipmi):
        drv, calls = ipmi
        drv.power_on()
        argv = calls[-1]
        assert argv[:1] == ["/usr/bin/ipmitool"] or argv[0] == "ipmitool"
        assert "-I" in argv and "lanplus" in argv
        assert "-H" in argv and "10.0.1.99" in argv
        assert "-E" in argv                    # password via IPMI_PASSWORD env
        assert "calvin" not in argv            # never in the process argv

    def test_cipher_and_port_flags(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(ipmi_mod.shutil, "which", lambda _n: "ipmitool")
        monkeypatch.setattr(ipmi_mod.subprocess, "run",
                            lambda argv, **kw: calls.append(argv) or
                            types.SimpleNamespace(returncode=0, stdout="Chassis Power is on\n", stderr=""))
        drv = IpmiDriver("h", "u", "p", cipher=17, port=6230, confirm=lambda *a: True)
        drv.is_powered_on()
        assert "-C" in calls[-1] and "17" in calls[-1]
        assert "-p" in calls[-1] and "6230" in calls[-1]


class TestPower:
    def test_is_powered_on(self, ipmi):
        drv, _ = ipmi
        assert drv.is_powered_on() is True

    def test_is_powered_off(self, ipmi):
        drv, _ = ipmi
        drv._outputs[0] = ("chassis power status", POWER_OFF)
        assert drv.is_powered_on() is False

    @pytest.mark.parametrize(
        "method,verb",
        [("power_on", "on"), ("power_off", "soft"),
         ("power_off_hard", "off"), ("reset_hard", "reset")],
    )
    def test_power_verbs(self, ipmi, method, verb):
        drv, calls = ipmi
        getattr(drv, method)()
        assert calls[-1][-3:] == ["chassis", "power", verb]

    def test_dry_run_sends_nothing(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(ipmi_mod.shutil, "which", lambda _n: "ipmitool")
        monkeypatch.setattr(ipmi_mod.subprocess, "run",
                            lambda argv, **kw: calls.append(argv))
        drv = IpmiDriver("h", "u", "p", dry_run=True)
        drv.power_on()
        assert calls == []                     # gated + skipped

    def test_denied_confirm_raises(self, monkeypatch):
        monkeypatch.setattr(ipmi_mod.shutil, "which", lambda _n: "ipmitool")
        monkeypatch.setattr(ipmi_mod.subprocess, "run", lambda *a, **k: pytest.fail("ran"))
        drv = IpmiDriver("h", "u", "p", confirm=deny_all)
        with pytest.raises(SafetyError):
            drv.power_on()


class TestSystemInfo:
    def test_get_info_parses_fru_and_mc(self, ipmi):
        drv, _ = ipmi
        info = drv.get_info()
        assert info["manufacturer"] == "DELL"
        # model comes from Board Product, NOT the "localhost" Product Name placeholder.
        assert info["model"] == "PowerEdge R710"
        assert info["serial_number"] == "ABC12345"
        assert info["power_state"] == "on"
        assert info["bmc_version"] == "1.85"

    def test_placeholder_product_name_does_not_mask_model(self, ipmi):
        # Regression for the real iDRAC6 finding: Product Name="localhost" must be
        # skipped so the model resolves to the Board Product, never "localhost".
        drv, _ = ipmi
        assert drv.get_info()["model"] != "localhost"

    def test_firmware_info_has_vendor_product(self, ipmi):
        # The run ledger / firmware registry join on vendor+product; without this
        # test-report recorded IPMI identity as fake/fake (the R710 row was
        # hand-authored to work around it). Now derived from FRU/MC.
        drv, _ = ipmi
        fw = drv.get_firmware_info()
        assert fw["vendor"] == "DELL"
        assert fw["product"] == "PowerEdge R710"
        assert fw["version"] == "1.85"


class TestBootConfig:
    def test_get_boot_options_pxe_once_efi(self, ipmi):
        drv, _ = ipmi
        o = drv.get_boot_options()
        assert o["target"] == "pxe"
        assert o["once"] is True and o["persistent"] is False
        assert o["mode"] == "UEFI"

    def test_get_boot_options_disk_persistent_legacy(self, ipmi):
        drv, _ = ipmi
        drv._outputs[2] = ("chassis bootparam get 5", BOOT_DISK_PERSIST_LEGACY)
        o = drv.get_boot_options()
        assert o["target"] == "hdd"
        assert o["persistent"] is True
        assert o["mode"] == "Legacy"

    def test_set_pxe_once_efi(self, ipmi):
        drv, calls = ipmi
        drv.set_boot_device("pxe", once=True, uefi=True)
        setcall = next(c for c in calls if "bootdev" in c)
        assert setcall[-2:] == ["pxe", "options=efiboot"]

    def test_set_hdd_persistent_legacy(self, ipmi):
        drv, calls = ipmi
        drv.set_boot_device("hdd", once=False, uefi=False)
        setcall = next(c for c in calls if "bootdev" in c)
        assert setcall[-2:] == ["disk", "options=persistent"]

    def test_set_none_clears(self, ipmi):
        drv, calls = ipmi
        drv.set_boot_device("none")
        setcall = next(c for c in calls if "bootdev" in c)
        assert setcall[-1] == "none"           # no options for a clear

    def test_usb_rejected(self, ipmi):
        drv, calls = ipmi
        with pytest.raises(KVMPilotError):
            drv.set_boot_device("usb")          # IPMI has no usb bootdev
        assert not any("bootdev" in c for c in calls)


class TestSensorsAndLogs:
    def test_read_sensors(self, ipmi):
        drv, _ = ipmi
        s = drv.read_sensors()
        assert s["count"] == 3
        names = {r["name"] for r in s["sensors"]}
        assert "CPU1 Temp" in names and "FAN1 RPM" in names

    def test_get_logs_returns_sel(self, ipmi):
        drv, _ = ipmi
        assert "Power off/down" in drv.get_logs()

    def test_get_logs_follow_unsupported(self, ipmi):
        drv, _ = ipmi
        with pytest.raises(CapabilityError):
            drv.get_logs(follow=True)


class TestSerialConsole:
    """SOL (serial-over-LAN) — PTY-backed ipmitool `sol activate`, faked (#208)."""

    @pytest.fixture()
    def sol(self, monkeypatch):
        """A driver whose SOL PTY + ipmitool child are faked; returns
        (driver, state) where state exposes popen argv, writes, and read queue."""
        import types as _types

        state = _types.SimpleNamespace(
            argv=None, writes=[], read_q=[b"grub> ", b""], runs=[], master=7, terminated=False
        )

        def fake_run(argv, **kw):  # the read-only _run() path (sol deactivate, etc.)
            state.runs.append(argv)
            return _types.SimpleNamespace(returncode=0, stdout="", stderr="")

        class FakeProc:
            def __init__(self, argv, **kw):
                state.argv = argv

            def poll(self):
                return None

            def terminate(self):
                state.terminated = True

            def wait(self, timeout=None):
                return 0

            def kill(self):
                state.terminated = True

        import pty

        monkeypatch.setattr(ipmi_mod.shutil, "which", lambda _n: "/usr/bin/ipmitool")
        monkeypatch.setattr(ipmi_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(ipmi_mod.subprocess, "Popen", FakeProc)
        monkeypatch.setattr(pty, "openpty", lambda: (state.master, 99))
        monkeypatch.setattr(ipmi_mod.os, "close", lambda fd: None)
        monkeypatch.setattr(ipmi_mod.os, "set_blocking", lambda fd, b: None)
        monkeypatch.setattr(
            ipmi_mod.os, "write", lambda fd, data: state.writes.append((fd, data)) or len(data)
        )
        monkeypatch.setattr(ipmi_mod.os, "read", lambda fd, n: state.read_q.pop(0) if state.read_q else b"")
        monkeypatch.setattr(
            ipmi_mod.select, "select",
            lambda r, w, x, t: (r, [], []) if state.read_q else ([], [], []),
        )
        drv = IpmiDriver("10.0.1.99", "root", "calvin", confirm=lambda *a: True)
        return drv, state

    def test_capability_and_protocol(self, sol):
        drv, _ = sol
        assert Capability.SERIAL_CONSOLE in drv.capabilities()
        assert isinstance(drv, SerialConsole)

    def test_activate_builds_sol_argv_over_pty(self, sol):
        drv, state = sol
        drv.serial_write("x")
        assert state.argv[-2:] == ["sol", "activate"]
        assert "-E" in state.argv and "calvin" not in state.argv  # password via env
        # single-session: a stale session is freed before activate
        assert ["sol", "deactivate"] == state.runs[0][-2:]

    def test_serial_write_sends_keystrokes_to_master(self, sol):
        drv, state = sol
        drv.serial_write("root\r")
        assert state.writes[-1] == (state.master, b"root\r")

    def test_serial_read_drains_console_output(self, sol):
        drv, _ = sol
        out = drv.serial_read(timeout=0.2)
        assert "grub>" in out

    def test_session_is_reused_not_reopened(self, sol):
        drv, state = sol
        drv.serial_write("a")
        drv.serial_write("b")  # second write must NOT spawn a second sol activate
        assert sum(1 for r in state.runs if r[-2:] == ["sol", "deactivate"]) == 1

    def test_dry_run_opens_nothing(self, monkeypatch):
        monkeypatch.setattr(ipmi_mod.shutil, "which", lambda _n: "ipmitool")
        monkeypatch.setattr(ipmi_mod.subprocess, "Popen",
                            lambda *a, **k: pytest.fail("opened SOL under dry-run"))
        drv = IpmiDriver("h", "u", "p", dry_run=True)
        assert drv.serial_read(timeout=0.1) == ""

    def test_denied_confirm_raises(self, monkeypatch):
        monkeypatch.setattr(ipmi_mod.shutil, "which", lambda _n: "ipmitool")
        drv = IpmiDriver("h", "u", "p", confirm=deny_all)
        with pytest.raises(SafetyError):
            drv.serial_write("x")

    def test_interactive_execs_ipmitool_sol_activate(self, sol):
        drv, state = sol
        rc = drv.serial_interactive()
        assert rc == 0
        assert state.runs[-1][-2:] == ["sol", "activate"]  # inherited-stdio subprocess.run

    def test_close_tears_down_and_frees_channel(self, sol):
        drv, state = sol
        drv.serial_write("x")           # open
        drv.serial_close()
        assert state.terminated is True
        assert state.runs[-1][-2:] == ["sol", "deactivate"]  # BMC channel freed
        assert drv._sol is None and drv._sol_fd is None


class TestErrorsAndRegistry:
    def test_ipmitool_missing_raises_capability_error(self, monkeypatch):
        monkeypatch.setattr(ipmi_mod.shutil, "which", lambda _n: None)
        drv = IpmiDriver("h", "u", "p", confirm=lambda *a: True)
        with pytest.raises(CapabilityError):
            drv.is_powered_on()

    def test_nonzero_exit_raises(self, monkeypatch):
        monkeypatch.setattr(ipmi_mod.shutil, "which", lambda _n: "ipmitool")
        monkeypatch.setattr(ipmi_mod.subprocess, "run",
                            lambda *a, **k: types.SimpleNamespace(
                                returncode=1, stdout="", stderr="Unable to establish LAN session"))
        drv = IpmiDriver("h", "u", "p", confirm=lambda *a: True)
        with pytest.raises(KVMPilotError):
            drv.is_powered_on()

    def test_make_driver_ipmi(self):
        assert isinstance(make_driver("ipmi", host="h"), IpmiDriver)

    def test_make_driver_from_config(self):
        from kvm_pilot.config import HostConfig
        cfg = HostConfig(host="h", driver="ipmi", user="root", passwd="calvin", ipmi_cipher=17)
        drv = make_driver_from_config(cfg)
        assert isinstance(drv, IpmiDriver)
        assert drv._cipher == 17
