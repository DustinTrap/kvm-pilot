"""Tests for Wake-on-LAN (#199).

Packet construction and ethtool parsing are pure/unit-tested; the single socket
call is exercised with a fake socket. The ethtool fixture is real output
captured from the .20 homelab host's wired NIC (eno1, Intel I219-LM).
"""

import socket

import pytest

from kvm_pilot import wol

MAC = "5c:60:ba:bb:cf:63"          # real .20-host wired NIC
MAC_RAW = bytes.fromhex("5c60babbcf63")


class TestNormalizeMac:
    def test_colon_form(self):
        assert wol.normalize_mac(MAC) == MAC_RAW

    def test_dash_bare_dot_forms_all_equal(self):
        assert wol.normalize_mac("5C-60-BA-BB-CF-63") == MAC_RAW
        assert wol.normalize_mac("5c60babbcf63") == MAC_RAW
        assert wol.normalize_mac("5c60.babb.cf63") == MAC_RAW  # Cisco dotted

    def test_case_and_whitespace_insensitive(self):
        assert wol.normalize_mac("  5C:60:BA:BB:CF:63 ") == MAC_RAW

    @pytest.mark.parametrize(
        "bad",
        ["", "5c:60:ba:bb:cf", "5c:60:ba:bb:cf:63:00", "zz:60:ba:bb:cf:63", "notamac"],
    )
    def test_invalid_raises(self, bad):
        with pytest.raises(ValueError):
            wol.normalize_mac(bad)

    def test_non_string_raises(self):
        with pytest.raises(ValueError):
            wol.normalize_mac(b"\x5c\x60\xba\xbb\xcf\x63")  # type: ignore[arg-type]


class TestMagicPacket:
    def test_length_prefix_and_repeat(self):
        pkt = wol.build_magic_packet(MAC)
        assert len(pkt) == 102
        assert pkt[:6] == b"\xff" * 6
        body = pkt[6:]
        assert body == MAC_RAW * 16
        # every 6-byte block after the prefix is the MAC
        assert all(body[i : i + 6] == MAC_RAW for i in range(0, 96, 6))


class _FakeSock:
    def __init__(self, log):
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def setsockopt(self, level, opt, val):
        self._log.append(("opt", level, opt, val))

    def bind(self, addr):
        self._log.append(("bind", addr))

    def sendto(self, data, addr):
        self._log.append(("sendto", data, addr))


class TestSend:
    def test_broadcasts_packet_count_times(self, monkeypatch):
        log: list = []
        monkeypatch.setattr(wol.socket, "socket", lambda *a, **k: _FakeSock(log))
        pkt = wol.send_magic_packet(MAC, broadcast="10.0.1.255", port=9, count=2)
        sends = [e for e in log if e[0] == "sendto"]
        assert len(sends) == 2
        assert sends[0][1] == pkt
        assert sends[0][2] == ("10.0.1.255", 9)

    def test_enables_so_broadcast(self, monkeypatch):
        log: list = []
        monkeypatch.setattr(wol.socket, "socket", lambda *a, **k: _FakeSock(log))
        wol.send_magic_packet(MAC)
        assert any(
            e[0] == "opt" and e[2] == socket.SO_BROADCAST and e[3] == 1 for e in log
        )

    def test_binds_interface_ip_when_given(self, monkeypatch):
        log: list = []
        monkeypatch.setattr(wol.socket, "socket", lambda *a, **k: _FakeSock(log))
        wol.send_magic_packet(MAC, interface_ip="10.0.1.50")
        assert ("bind", ("10.0.1.50", 0)) in log

    def test_no_bind_without_interface_ip(self, monkeypatch):
        log: list = []
        monkeypatch.setattr(wol.socket, "socket", lambda *a, **k: _FakeSock(log))
        wol.send_magic_packet(MAC)
        assert not any(e[0] == "bind" for e in log)

    def test_invalid_count_raises(self):
        with pytest.raises(ValueError):
            wol.send_magic_packet(MAC, count=0)

    def test_bad_mac_raises_before_socket(self, monkeypatch):
        # A malformed MAC must fail without ever opening a socket.
        monkeypatch.setattr(
            wol.socket, "socket", lambda *a, **k: pytest.fail("socket opened")
        )
        with pytest.raises(ValueError):
            wol.send_magic_packet("nope")


class TestParseEthtoolWol:
    # Real capture from the .20 host wired NIC (eno1) during the SNO build-out.
    REAL_FIXTURE = (
        "Settings for eno1:\n"
        "\tSupported ports: [ TP ]\n"
        "\tSupports Wake-on: pumbg\n"
        "\tWake-on: g\n"
        "\tLink detected: no\n"
    )

    def test_real_fixture_magic_supported_and_enabled(self):
        r = wol.parse_ethtool_wol(self.REAL_FIXTURE)
        assert r["supported"] == "pumbg"
        assert r["current"] == "g"
        assert r["supports_magic"] is True
        assert r["magic_enabled"] is True

    def test_disabled_state(self):
        r = wol.parse_ethtool_wol("\tSupports Wake-on: pumbg\n\tWake-on: d\n")
        assert r["supports_magic"] is True
        assert r["magic_enabled"] is False

    def test_no_wol_support(self):
        r = wol.parse_ethtool_wol("\tSupports Wake-on: d\n\tWake-on: d\n")
        assert r["supports_magic"] is False
        assert r["magic_enabled"] is False

    def test_empty_output(self):
        r = wol.parse_ethtool_wol("")
        assert r == {
            "supported": "",
            "current": "",
            "supports_magic": False,
            "magic_enabled": False,
        }
