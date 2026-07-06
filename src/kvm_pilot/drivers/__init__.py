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

from ..errors import CapabilityError, KVMPilotError
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
    from ..client import KVMClient
    from ..config import HostConfig
    from .fake import FakeDriver
    from .glkvm import GLKVMDriver
    from .pikvm import BliKVMDriver
    from .redfish import RedfishDriver


# -- driver registry -------------------------------------------------------
#
# Factories lazy-import their driver module so that importing this package never
# drags in the client (which itself imports from ``.base``) or the fake driver —
# avoiding an import cycle and keeping ``import kvm_pilot`` lean.


def _make_pikvm(**conf: object) -> KVMDriver:
    from ..client import PiKVMDriver

    return PiKVMDriver(**conf)  # type: ignore[arg-type]


def _make_glkvm(**conf: object) -> KVMDriver:
    from .glkvm import GLKVMDriver

    return GLKVMDriver(**conf)  # type: ignore[arg-type]


def _make_blikvm(**conf: object) -> KVMDriver:
    from .pikvm import BliKVMDriver

    return BliKVMDriver(**conf)  # type: ignore[arg-type]


def _make_fake(**conf: object) -> KVMDriver:
    from .fake import FakeDriver

    return FakeDriver(**conf)  # type: ignore[arg-type]


def _make_redfish(**conf: object) -> KVMDriver:
    from .redfish import RedfishDriver

    return RedfishDriver(**conf)  # type: ignore[arg-type]


_DRIVER_FACTORIES: dict[str, Callable[..., KVMDriver]] = {
    # PiKVM-family: one base (PiKVMDriver) with thin GLKVM/BliKVM subclasses for
    # the API-compatible forks.
    "pikvm": _make_pikvm,
    "glkvm": _make_glkvm,
    "blikvm": _make_blikvm,
    # One Redfish driver covers Dell iDRAC, HPE iLO, Supermicro, Lenovo XCC, OpenBMC.
    "redfish": _make_redfish,
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
      ``"redfish"`` -> :class:`~kvm_pilot.drivers.redfish.RedfishDriver` (DMTF
      Redfish BMCs: Dell iDRAC, HPE iLO, Supermicro, Lenovo XCC, OpenBMC).
      ``"fake"`` -> :class:`FakeDriver` (in-process, no hardware).
    """
    factory = _DRIVER_FACTORIES.get(kind.lower())
    if factory is None:
        known = ", ".join(sorted(_DRIVER_FACTORIES))
        raise ValueError(f"Unknown driver kind: {kind!r}. Known kinds: {known}")
    return factory(**conf)


def make_driver_from_config(
    cfg: HostConfig, *, confirm: Callable[[str, str], bool] | None = None, dry_run: bool = False
) -> KVMClient | FakeDriver | RedfishDriver:
    """Build the driver named by ``cfg.driver`` from a resolved ``HostConfig``.

    Shared by the CLI and the MCP server so a profile/env that pins
    ``driver = "glkvm"`` is honored everywhere — not just via ``--driver``. It is
    shape-aware (the fake driver takes no credentials; the PiKVM family and the
    Redfish BMC build via ``from_config``), unlike a raw ``make_driver(**all_fields)``
    call.
    """
    kind = cfg.driver
    if kind == "fake":
        from .fake import FakeDriver

        drv: KVMClient | FakeDriver | RedfishDriver = FakeDriver(
            host=cfg.host, confirm=confirm, dry_run=dry_run
        )
    elif kind in ("pikvm", "glkvm", "blikvm"):
        from ..client import PiKVMDriver
        from .glkvm import GLKVMDriver
        from .pikvm import BliKVMDriver

        cls = {"pikvm": PiKVMDriver, "glkvm": GLKVMDriver, "blikvm": BliKVMDriver}[kind]
        drv = cls.from_config(cfg, confirm=confirm, dry_run=dry_run)
    elif kind == "redfish":
        from .redfish import RedfishDriver

        drv = RedfishDriver.from_config(cfg, confirm=confirm, dry_run=dry_run)
    else:
        raise KVMPilotError(
            f"Driver {kind!r} does not support from-config construction here "
            "(supported: pikvm, glkvm, blikvm, redfish, fake). Build it directly with "
            f"make_driver({kind!r}, ...) from the library."
        )
    # Attach the in-band SSH channel to the managed host's OS when the profile
    # configures one, so the healthcheck can probe reachability (#81). It adds no
    # RemoteShell methods, so detect_capabilities is unaffected — SSH-to-target
    # stays a per-profile channel, not a driver capability.
    if cfg.ssh_host:
        from ..ssh import SSHChannel

        try:
            drv.ssh_channel = SSHChannel.from_config(cfg)  # type: ignore[union-attr]
        except CapabilityError:
            pass  # a malformed ssh_host must never break KVM operation
    return drv


def __getattr__(name: str) -> object:
    # Lazily expose the concrete drivers without importing them at package load
    # (keeps the import graph acyclic). ``from kvm_pilot.drivers import FakeDriver``
    # and ``... import RedfishDriver`` both work.
    if name == "FakeDriver":
        from .fake import FakeDriver

        return FakeDriver
    if name == "RedfishDriver":
        from .redfish import RedfishDriver

        return RedfishDriver
    if name == "GLKVMDriver":
        from .glkvm import GLKVMDriver

        return GLKVMDriver
    if name == "BliKVMDriver":
        from .pikvm import BliKVMDriver

        return BliKVMDriver
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
    "make_driver_from_config",
    "register_driver",
    "FakeDriver",
    "RedfishDriver",
    "GLKVMDriver",
    "BliKVMDriver",
]
