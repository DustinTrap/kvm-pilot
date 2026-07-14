"""Wake-on-LAN: build/send magic packets and parse ``ethtool`` WoL state.

WoL is the power-*on* path for targets whose KVM appliance has no wired ATX or
GPIO power channel — the CRITICAL "no out-of-band reset" healthcheck finding
(#199). A magic packet is ``6x 0xFF`` followed by the target MAC repeated 16
times (102 bytes); broadcast over UDP, a NIC armed for WoL (``ethtool`` shows
``Wake-on: g``) powers the host on when it sees the packet.

This module is intentionally dependency-free and side-effect-light so the packet
construction and ``ethtool`` parsing are unit-testable without hardware; the one
socket call is isolated in :func:`send_magic_packet`.
"""

from __future__ import annotations

import socket

# UDP discard port; 7 (echo) and 0 are also seen in the wild. The payload, not
# the port, is what a NIC matches on, so any works — 9 is the de-facto default.
DEFAULT_WOL_PORT = 9
_MAGIC_PREFIX = b"\xff" * 6
_PACKET_LEN = 6 + 6 * 16  # 102


def normalize_mac(mac: str) -> bytes:
    """Parse a MAC in colon/dash/dot/bare-hex form into 6 raw bytes.

    Accepts ``5c:60:ba:bb:cf:63``, ``5C-60-BA-BB-CF-63``, ``5c60.babb.cf63``
    (Cisco), or ``5c60babbcf63`` (case/space-insensitive). Raises ``ValueError``
    on anything that is not exactly six hex octets.
    """
    if not isinstance(mac, str):
        raise ValueError(f"MAC must be a string, got {type(mac).__name__}")
    cleaned = mac.strip().lower().replace(":", "").replace("-", "").replace(".", "")
    if len(cleaned) != 12 or any(c not in "0123456789abcdef" for c in cleaned):
        raise ValueError(f"invalid MAC address: {mac!r}")
    return bytes.fromhex(cleaned)


def build_magic_packet(mac: str) -> bytes:
    """Return the 102-byte Wake-on-LAN magic packet for ``mac``."""
    return _MAGIC_PREFIX + normalize_mac(mac) * 16


def send_magic_packet(
    mac: str,
    *,
    broadcast: str = "255.255.255.255",
    port: int = DEFAULT_WOL_PORT,
    count: int = 1,
    interface_ip: str | None = None,
) -> bytes:
    """Broadcast a WoL magic packet for ``mac``; return the packet sent.

    ``broadcast`` should be the target segment's broadcast address (e.g.
    ``10.0.1.255``) so the frame reaches the sleeping NIC's L2. ``interface_ip``
    binds the send to a specific local NIC on a multi-homed sender. ``count``
    sends the packet more than once (WoL is fire-and-forget; a couple of copies
    hedges against loss).
    """
    if count < 1:
        raise ValueError("count must be >= 1")
    packet = build_magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if interface_ip:
            sock.bind((interface_ip, 0))
        for _ in range(count):
            sock.sendto(packet, (broadcast, port))
    return packet


def parse_ethtool_wol(ethtool_output: str) -> dict:
    """Parse ``ethtool <iface>`` output for WoL capability and current state.

    Returns ``{'supported': str, 'current': str, 'supports_magic': bool,
    'magic_enabled': bool}``. ethtool's letter codes: ``g`` = wake on
    MagicPacket, ``d`` = disabled (see ``man ethtool``).
    """
    supported = ""
    current = ""
    for line in ethtool_output.splitlines():
        s = line.strip()
        if s.startswith("Supports Wake-on:"):
            supported = s.split(":", 1)[1].strip()
        elif s.startswith("Wake-on:"):
            current = s.split(":", 1)[1].strip()
    return {
        "supported": supported,
        "current": current,
        "supports_magic": "g" in supported,
        "magic_enabled": "g" in current,
    }
