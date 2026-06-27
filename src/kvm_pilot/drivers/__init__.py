"""Device drivers and the capability model for kvm-pilot.

The capability protocols live in :mod:`kvm_pilot.drivers.base`. This package
also hosts the driver registry — ``make_driver(kind, **conf)`` mirrors the
vision layer's ``make_backend`` — and the built-in drivers: the PiKVM-family
REST client (``KVMClient``) and the hardware-free :class:`FakeDriver`. See
``docs/architecture.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from .fake import FakeDriver


# -- driver registry -------------------------------------------------------
#
# Factories lazy-import their driver module so that importing this package never
# drags in the client (which itself imports from ``.base``) or the fake driver —
# avoiding an import cycle and keeping ``import kvm_pilot`` lean.


def _make_pikvm(**conf: object) -> KVMDriver:
    from ..client import KVMClient

    return KVMClient(**conf)  # type: ignore[arg-type]


def _make_fake(**conf: object) -> KVMDriver:
    from .fake import FakeDriver

    return FakeDriver(**conf)  # type: ignore[arg-type]


_DRIVER_FACTORIES: dict[str, Callable[..., KVMDriver]] = {
    # PiKVM, the GL.iNet GLKVM fork, and BliKVM share one API-compatible client.
    "pikvm": _make_pikvm,
    "glkvm": _make_pikvm,
    "blikvm": _make_pikvm,
    "fake": _make_fake,
}


def register_driver(kind: str, factory: Callable[..., KVMDriver]) -> None:
    """Register a driver factory under ``kind``.

    Lets a third-party driver plug in without forking the core; an entry-point
    discovery group is a later step (see ``docs/architecture.md``).
    """
    _DRIVER_FACTORIES[kind.lower()] = factory


def make_driver(kind: str = "pikvm", **conf: object) -> KVMDriver:
    """Build a device driver by name, mirroring ``vision.make_backend``.

    Built-in kinds:
      ``"pikvm"`` / ``"glkvm"`` / ``"blikvm"`` -> ``KVMClient`` (PiKVM-family
      REST client; pass ``host=`` and credentials).
      ``"fake"`` -> :class:`FakeDriver` (in-process, no hardware).
    """
    factory = _DRIVER_FACTORIES.get(kind.lower())
    if factory is None:
        known = ", ".join(sorted(_DRIVER_FACTORIES))
        raise ValueError(f"Unknown driver kind: {kind!r}. Known kinds: {known}")
    return factory(**conf)


def __getattr__(name: str) -> object:
    # Lazily expose FakeDriver without importing it at package load (keeps the
    # import graph acyclic). ``from kvm_pilot.drivers import FakeDriver`` works.
    if name == "FakeDriver":
        from .fake import FakeDriver

        return FakeDriver
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    "make_driver",
    "register_driver",
    "FakeDriver",
]
