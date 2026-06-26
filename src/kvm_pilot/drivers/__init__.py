"""Device drivers and the capability model for kvm-pilot.

Step 1 of the driver-plugin refactor: the capability protocols live in
:mod:`kvm_pilot.drivers.base`. Concrete drivers and a ``make_driver`` registry
land in later steps (see ``docs/architecture.md``).
"""

from __future__ import annotations

from .base import (
    CAPABILITY_PROTOCOLS,
    GPIO,
    HID,
    BootProgress,
    Capability,
    CapabilityMixin,
    Events,
    KVMDriver,
    Logs,
    Power,
    Sensors,
    SerialConsole,
    SystemInfo,
    Video,
    VirtualMedia,
    Watchdog,
    detect_capabilities,
)

__all__ = [
    "Capability",
    "CapabilityMixin",
    "KVMDriver",
    "SystemInfo",
    "Power",
    "HID",
    "Video",
    "VirtualMedia",
    "GPIO",
    "Events",
    "Logs",
    "BootProgress",
    "Sensors",
    "SerialConsole",
    "Watchdog",
    "CAPABILITY_PROTOCOLS",
    "detect_capabilities",
]
