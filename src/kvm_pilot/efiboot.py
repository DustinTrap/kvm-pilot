"""Parse ``efibootmgr`` output and pick a boot entry for a normalized device.

The in-band counterpart to the Redfish BootSourceOverride path (#150/#201): on a
host with a running OS (reached over the SSH channel), ``efibootmgr`` lists the
UEFI boot entries and ``efibootmgr -n <id>`` sets a one-time ``BootNext``. This
module is the pure, hardware-free core — parsing and device-to-entry matching —
so it is unit-tested against captured fixtures; the SSH execution + gating live in
the CLI (``boot-device --via ssh``).
"""

from __future__ import annotations

import re

_BOOT_LINE = re.compile(r"^Boot([0-9A-Fa-f]{4})(\*?)\s+(.*)$")

# Normalized device token -> ordered label/path regexes (first hit wins). Matched
# against the whole entry text (label + any device path from ``-v``), so signals
# in the path — NVMe, MAC/IPv4, USB() — count too. Order encodes preference
# (e.g. IPv4 PXE before IPv6; the OS loader before a raw disk).
_MATCH_PATTERNS: dict[str, list[str]] = {
    "pxe": [r"ipv4.*network|network.*ipv4|\bpxe\b", r"\bnetwork\b|ipv6"],
    "usb": [r"\busb\b(?!.*network)"],  # a USB volume, not "USB NETWORK BOOT" (PXE-over-USB-NIC)
    "cd": [r"\bcd\b|\bdvd\b|cd/dvd|cdrom|optical"],
    "hdd": [
        r"windows boot manager|\bredhat\b|ubuntu|fedora|debian|grub|rhel|opensuse",
        r"nvme|\bsata\b|\bssd\b|\bhdd\b|hard drive|\bata\b|\bscsi\b",
    ],
}

VALID_DEVICES = ("pxe", "cd", "hdd", "usb")


def parse_efibootmgr(output: str) -> dict:
    """Parse ``efibootmgr`` (plain or ``-v``) output.

    Returns ``{current, order, timeout, entries, active}`` where ``entries`` maps
    a 4-hex-digit id (uppercased) to its text, ``active`` maps id -> bool (the
    ``*`` flag), ``order`` is the BootOrder id list, ``current`` is BootCurrent.
    """
    current: str | None = None
    order: list[str] = []
    timeout: int | None = None
    entries: dict[str, str] = {}
    active: dict[str, bool] = {}
    for raw in output.splitlines():
        line = raw.rstrip()
        if line.startswith("BootCurrent:"):
            current = line.split(":", 1)[1].strip().upper() or None
        elif line.startswith("BootOrder:"):
            order = [x.strip().upper() for x in line.split(":", 1)[1].split(",") if x.strip()]
        elif line.startswith("Timeout:"):
            m = re.search(r"\d+", line)
            timeout = int(m.group()) if m else None
        else:
            m = _BOOT_LINE.match(line)
            if m:
                bid = m.group(1).upper()
                entries[bid] = m.group(3).strip()
                active[bid] = m.group(2) == "*"
    return {
        "current": current,
        "order": order,
        "timeout": timeout,
        "entries": entries,
        "active": active,
    }


def match_boot_entry(entries: dict[str, str], device: str) -> str | None:
    """Return the boot-entry id best matching a normalized ``device``, or None.

    Deterministic: patterns are tried in preference order, and within a pattern
    entries are scanned in id order. ``None`` means the box has no such entry (the
    caller should surface the available entries so an operator can pick explicitly).
    """
    key = device.strip().lower()
    patterns = _MATCH_PATTERNS.get(key)
    if patterns is None:
        raise ValueError(f"unknown boot device {device!r}; choose one of {list(VALID_DEVICES)}")
    for pattern in patterns:
        rx = re.compile(pattern, re.IGNORECASE)
        for bid in sorted(entries):
            if rx.search(entries[bid]):
                return bid
    return None


def set_boot_next_command(entry_id: str) -> str:
    """The command that sets a one-time BootNext to ``entry_id`` (validated hex)."""
    bid = entry_id.strip().upper()
    if not re.fullmatch(r"[0-9A-F]{4}", bid):
        raise ValueError(f"invalid boot entry id {entry_id!r} (want 4 hex digits)")
    return f"efibootmgr -n {bid}"
