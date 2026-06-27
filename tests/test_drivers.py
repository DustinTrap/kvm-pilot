"""Capability-protocol conformance for the driver layer (step 1)."""

from __future__ import annotations

from kvm_pilot import KVMClient
from kvm_pilot.drivers.base import (
    GPIO,
    HID,
    BootProgress,
    Capability,
    Events,
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

# Protocols the PiKVM client implements today: the original seven plus Logs
# (/api/log). The remaining sensing protocols are the seam for future BMC
# drivers (Redfish/IPMI), so the PiKVM client does not satisfy them.
ALL_PROTOCOLS = (SystemInfo, Power, HID, Video, VirtualMedia, GPIO, Events, Logs)

FORWARD_LOOKING_PROTOCOLS = (BootProgress, Sensors, SerialConsole, Watchdog)

PIKVM_CAPABILITIES = {
    Capability.SYSTEM_INFO,
    Capability.POWER,
    Capability.HID,
    Capability.VIDEO,
    Capability.VIRTUAL_MEDIA,
    Capability.GPIO,
    Capability.EVENTS,
    Capability.LOGS,
}


def make_client() -> KVMClient:
    # TEST-NET-1 address (RFC 5737); constructing a client performs no network I/O.
    return KVMClient("192.0.2.1")


def test_pikvm_client_satisfies_every_capability_protocol() -> None:
    client = make_client()
    for proto in ALL_PROTOCOLS:
        assert isinstance(client, proto), proto.__name__


def test_capabilities_reports_the_pikvm_subset() -> None:
    assert make_client().capabilities() == PIKVM_CAPABILITIES


def test_forward_looking_sensing_caps_unsupported_by_pikvm() -> None:
    client = make_client()
    for proto in FORWARD_LOOKING_PROTOCOLS:
        assert not isinstance(client, proto), proto.__name__
    for cap in (
        Capability.BOOT_PROGRESS,
        Capability.SENSORS,
        Capability.SERIAL_CONSOLE,
        Capability.WATCHDOG,
    ):
        assert not client.supports(cap)


def test_supports_accepts_enum_and_string() -> None:
    client = make_client()
    assert client.supports(Capability.POWER)
    assert client.supports("video")


def test_detect_capabilities_on_a_partial_driver() -> None:
    class PowerOnly:
        def power_on(self, wait: bool = True) -> None: ...
        def power_off(self, wait: bool = True) -> None: ...
        def power_off_hard(self, wait: bool = True) -> None: ...
        def reset_hard(self, wait: bool = True) -> None: ...

        def is_powered_on(self) -> bool:
            return True

    assert detect_capabilities(PowerOnly()) == {Capability.POWER}


# -- driver registry -------------------------------------------------------

def test_make_driver_pikvm_aliases_build_the_client() -> None:
    from kvm_pilot.drivers import make_driver

    for kind in ("pikvm", "glkvm", "blikvm", "PiKVM"):
        d = make_driver(kind, host="192.0.2.1")
        assert isinstance(d, KVMClient)
        assert d.host == "192.0.2.1"


def test_make_driver_fake() -> None:
    from kvm_pilot.drivers import FakeDriver, make_driver

    d = make_driver("fake", host="lab")
    assert isinstance(d, FakeDriver)
    assert d.host == "lab"


def test_make_driver_glkvm_blikvm_return_the_subclasses() -> None:
    from kvm_pilot.client import PiKVMDriver
    from kvm_pilot.drivers import BliKVMDriver, GLKVMDriver, make_driver

    g = make_driver("glkvm", host="h")
    b = make_driver("blikvm", host="h")
    assert isinstance(g, GLKVMDriver) and isinstance(g, PiKVMDriver)
    assert isinstance(b, BliKVMDriver) and isinstance(b, PiKVMDriver)
    # And they still satisfy the original public name.
    assert isinstance(g, KVMClient)


def test_make_driver_unknown_kind_lists_known() -> None:
    import pytest

    from kvm_pilot.drivers import make_driver

    with pytest.raises(ValueError, match="Unknown driver kind"):
        make_driver("nope")


def test_register_driver_adds_a_kind() -> None:
    from kvm_pilot.drivers import make_driver, register_driver

    sentinel = object()
    register_driver("sentinel", lambda **conf: sentinel)
    assert make_driver("sentinel") is sentinel


def test_make_driver_from_config_dispatches_on_cfg_driver() -> None:
    # The CLI and MCP server share this so cfg.driver is honored identically.
    import pytest

    from kvm_pilot.config import HostConfig
    from kvm_pilot.drivers import FakeDriver, make_driver_from_config
    from kvm_pilot.drivers.pikvm import GLKVMDriver
    from kvm_pilot.errors import KVMPilotError

    assert isinstance(make_driver_from_config(HostConfig(host="h", driver="glkvm")), GLKVMDriver)
    assert isinstance(make_driver_from_config(HostConfig(host="h", driver="fake")), FakeDriver)
    with pytest.raises(KVMPilotError, match="does not support"):
        make_driver_from_config(HostConfig(host="h", driver="redfish"))
