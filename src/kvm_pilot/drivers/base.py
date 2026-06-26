"""
Device-driver capability model for kvm-pilot.

Many KVM / BMC devices exist and they expose very different feature sets, so a
single monolithic interface is the wrong shape. Instead each capability is a
small, ``@runtime_checkable`` ``Protocol``: a driver implements only the ones
its hardware actually supports, and advertises them via ``capabilities()``.

This is **step 1** of the driver-plugin refactor — the protocols are defined
here and the existing PiKVM client (``KVMClient``) implements them unchanged via
``CapabilityMixin``. Concrete non-PiKVM drivers and a ``make_driver`` registry
come in later steps; see ``docs/architecture.md``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable


class Capability(StrEnum):
    """A feature area a KVM driver may or may not support."""

    SYSTEM_INFO = "system_info"
    POWER = "power"
    HID = "hid"
    VIDEO = "video"
    VIRTUAL_MEDIA = "virtual_media"
    GPIO = "gpio"
    EVENTS = "events"


@runtime_checkable
class SystemInfo(Protocol):
    """Read device / host information."""

    def get_info(self, fields: list | None = None) -> dict: ...


@runtime_checkable
class Power(Protocol):
    """Control and read host power state."""

    def power_on(self, wait: bool = True) -> None: ...
    def power_off(self, wait: bool = True) -> None: ...
    def power_off_hard(self, wait: bool = True) -> None: ...
    def reset_hard(self, wait: bool = True) -> None: ...
    def is_powered_on(self) -> bool: ...


@runtime_checkable
class HID(Protocol):
    """Emulate keyboard and mouse input."""

    def type_text(self, text: str) -> None: ...
    def press_key(self, key: str) -> None: ...
    def send_shortcut(self, keys: str) -> None: ...
    def mouse_move(self, x: int, y: int) -> None: ...
    def mouse_click(self, button: str = "left") -> None: ...


@runtime_checkable
class Video(Protocol):
    """Capture a still frame of the host console (feeds the vision layer)."""

    def snapshot(self) -> bytes: ...
    def snapshot_base64(self) -> str: ...


@runtime_checkable
class VirtualMedia(Protocol):
    """Attach / detach virtual media (ISO or USB image)."""

    def mount_iso(self, source: str) -> str: ...
    def msd_connect(self) -> None: ...
    def msd_disconnect(self) -> None: ...


@runtime_checkable
class GPIO(Protocol):
    """Drive GPIO channels (relays, power buttons, LEDs)."""

    def gpio_switch(self, channel: str, state: bool) -> None: ...
    def gpio_pulse(self, channel: str) -> None: ...


@runtime_checkable
class Events(Protocol):
    """Stream asynchronous device events."""

    def watch_events(self) -> object: ...


# Maps each capability to the protocol that defines it, so support can be
# detected structurally rather than declared by hand.
CAPABILITY_PROTOCOLS: dict[Capability, type] = {
    Capability.SYSTEM_INFO: SystemInfo,
    Capability.POWER: Power,
    Capability.HID: HID,
    Capability.VIDEO: Video,
    Capability.VIRTUAL_MEDIA: VirtualMedia,
    Capability.GPIO: GPIO,
    Capability.EVENTS: Events,
}


def detect_capabilities(obj: object) -> set[Capability]:
    """Return the set of capabilities ``obj`` structurally satisfies."""
    return {cap for cap, proto in CAPABILITY_PROTOCOLS.items() if isinstance(obj, proto)}


class CapabilityMixin:
    """Gives a driver ``capabilities()`` / ``supports()`` for free.

    Both are derived structurally from the capability protocols above, so a
    driver never hand-maintains a list — it simply implements the methods for
    the capabilities its hardware has.
    """

    def capabilities(self) -> set[Capability]:
        return detect_capabilities(self)

    def supports(self, capability: Capability | str) -> bool:
        return Capability(capability) in self.capabilities()


@runtime_checkable
class KVMDriver(Protocol):
    """Anything that exposes a host and reports its capabilities."""

    host: str

    def capabilities(self) -> set[Capability]: ...
    def supports(self, capability: Capability | str) -> bool: ...


__all__ = [
    "Capability",
    "SystemInfo",
    "Power",
    "HID",
    "Video",
    "VirtualMedia",
    "GPIO",
    "Events",
    "KVMDriver",
    "CapabilityMixin",
    "CAPABILITY_PROTOCOLS",
    "detect_capabilities",
]
