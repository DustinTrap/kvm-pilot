"""Tests for efibootmgr parsing + device matching (#150/#22).

Pure/hardware-free. REAL_FIXTURE is captured from the .16 homelab host
(Dell wired NIC I219-LM, Samsung NVMe, redhat shim); SYNTH_FIXTURE adds a CD/DVD
and a Windows Boot Manager to cover matches the real box lacks.
"""

from __future__ import annotations

import pytest

from kvm_pilot import efiboot

# Real `efibootmgr` output from 10.0.1.16 (trimmed device paths kept — they carry
# the NVMe/MAC/IPv4/USB signals the matcher can use).
REAL_FIXTURE = """\
BootCurrent: 0005
Timeout: 0 seconds
BootOrder: 0005,0001,0004,0002,0003,0006,0007
Boot0001* SAMSUNG MZVL2256HCHQ-00BH1-S63XNX0T339238\tPciRoot(0x0)/Pci(0x1b,0x0)/Pci(0x0,0x0)/NVMe(0x1,00-25-38-B3-21-C6-70-70)
Boot0002* IPV6 Network - Intel(R) Ethernet Connection (17) I219-LM\tPciRoot(0x0)/Pci(0x1f,0x6)/MAC(5c60babbcf63,0)/IPv6([::])
Boot0003* IPV4 Network - Intel(R) Ethernet Connection (17) I219-LM\tPciRoot(0x0)/Pci(0x1f,0x6)/MAC(5c60babbcf63,0)/IPv4(0.0.0.0)
Boot0004* USB\tPciRoot(0x0)/Pci(0x14,0x0)
Boot0005* redhat\tHD(1,GPT,ad2dec7d-34e4-4de5-8952-29bfea127409)/File(\\EFI\\redhat\\shimx64.efi)
Boot0006  USB NETWORK BOOT\tPciRoot(0x0)/Pci(0x0,0x0)/IPv4(0.0.0.0,DHCP)
Boot0007  USB NETWORK BOOT\tPciRoot(0x0)/Pci(0x0,0x0)/IPv6([::])
"""

SYNTH_FIXTURE = """\
BootCurrent: 0000
BootOrder: 0000,0001,0002,0003
Boot0000* Windows Boot Manager\tHD(1,GPT,abc)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)
Boot0001* UEFI: SanDisk Cruzer USB\tUSB(0x1)
Boot0002* UEFI: HL-DT-ST DVD-ROM\tCD/DVD
Boot0003* UEFI: PXE IPv4 Intel\tMAC(001122334455)/IPv4
"""


class TestParse:
    def test_headers_and_entries(self):
        r = efiboot.parse_efibootmgr(REAL_FIXTURE)
        assert r["current"] == "0005"
        assert r["timeout"] == 0
        assert r["order"] == ["0005", "0001", "0004", "0002", "0003", "0006", "0007"]
        assert set(r["entries"]) == {"0001", "0002", "0003", "0004", "0005", "0006", "0007"}
        assert r["entries"]["0005"].startswith("redhat")

    def test_active_flag(self):
        r = efiboot.parse_efibootmgr(REAL_FIXTURE)
        assert r["active"]["0005"] is True     # Boot0005*
        assert r["active"]["0006"] is False    # Boot0006 (no star)

    def test_empty_output(self):
        r = efiboot.parse_efibootmgr("")
        assert r == {"current": None, "order": [], "timeout": None, "entries": {}, "active": {}}


class TestMatchReal:
    @pytest.fixture()
    def entries(self):
        return efiboot.parse_efibootmgr(REAL_FIXTURE)["entries"]

    def test_pxe_prefers_ipv4(self, entries):
        assert efiboot.match_boot_entry(entries, "pxe") == "0003"  # IPV4 Network, not IPV6

    def test_usb_excludes_usb_network_boot(self, entries):
        assert efiboot.match_boot_entry(entries, "usb") == "0004"  # "USB", not "USB NETWORK BOOT"

    def test_hdd_prefers_os_loader(self, entries):
        assert efiboot.match_boot_entry(entries, "hdd") == "0005"  # redhat shim

    def test_cd_absent_returns_none(self, entries):
        assert efiboot.match_boot_entry(entries, "cd") is None


class TestMatchSynth:
    @pytest.fixture()
    def entries(self):
        return efiboot.parse_efibootmgr(SYNTH_FIXTURE)["entries"]

    def test_cd_matches_dvd(self, entries):
        assert efiboot.match_boot_entry(entries, "cd") == "0002"

    def test_hdd_matches_windows_boot_manager(self, entries):
        assert efiboot.match_boot_entry(entries, "hdd") == "0000"

    def test_usb_matches_removable(self, entries):
        assert efiboot.match_boot_entry(entries, "usb") == "0001"

    def test_pxe_matches(self, entries):
        assert efiboot.match_boot_entry(entries, "pxe") == "0003"


class TestMisc:
    def test_unknown_device_raises(self):
        with pytest.raises(ValueError):
            efiboot.match_boot_entry({}, "floppy")

    def test_set_boot_next_command(self):
        assert efiboot.set_boot_next_command("0003") == "efibootmgr -n 0003"

    def test_set_boot_next_command_normalizes_and_validates(self):
        assert efiboot.set_boot_next_command("00a3") == "efibootmgr -n 00A3"
        for bad in ("", "5", "zzzz", "00000"):
            with pytest.raises(ValueError):
                efiboot.set_boot_next_command(bad)
