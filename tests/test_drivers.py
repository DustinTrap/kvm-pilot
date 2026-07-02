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
    from kvm_pilot.drivers import FakeDriver, RedfishDriver, make_driver_from_config
    from kvm_pilot.drivers.pikvm import GLKVMDriver
    from kvm_pilot.errors import KVMPilotError

    assert isinstance(make_driver_from_config(HostConfig(host="h", driver="glkvm")), GLKVMDriver)
    assert isinstance(make_driver_from_config(HostConfig(host="h", driver="fake")), FakeDriver)
    # redfish now builds via RedfishDriver.from_config (construction only, lazy login).
    assert isinstance(
        make_driver_from_config(HostConfig(host="h", driver="redfish")), RedfishDriver
    )
    # A genuinely unknown kind still raises a clean, actionable error.
    with pytest.raises(KVMPilotError, match="does not support"):
        make_driver_from_config(HostConfig(host="h", driver="ipmi"))


# -- shared PowerMixin.hard_cycle (#63) ------------------------------------

def test_hard_cycle_is_shared_from_power_mixin():
    from kvm_pilot.client import PiKVMDriver
    from kvm_pilot.drivers.base import PowerMixin
    from kvm_pilot.drivers.fake import FakeDriver
    from kvm_pilot.drivers.redfish import RedfishDriver

    for cls in (PiKVMDriver, FakeDriver, RedfishDriver):
        assert issubclass(cls, PowerMixin)
        # none of them redefines hard_cycle — it comes from the mixin
        assert "hard_cycle" not in cls.__dict__


def test_hard_cycle_default_delays_per_driver():
    from kvm_pilot.client import PiKVMDriver
    from kvm_pilot.drivers.fake import FakeDriver
    from kvm_pilot.drivers.redfish import RedfishDriver

    # PiKVM (ATX, non-blocking) settles; Redfish/Fake (blocking/instant) do not.
    assert (PiKVMDriver._hard_cycle_off_delay, PiKVMDriver._hard_cycle_on_delay) == (5.0, 3.0)
    assert (RedfishDriver._hard_cycle_off_delay, RedfishDriver._hard_cycle_on_delay) == (0.0, 0.0)
    assert (FakeDriver._hard_cycle_off_delay, FakeDriver._hard_cycle_on_delay) == (0.0, 0.0)


def test_hard_cycle_composes_off_then_on_on_fake():
    from kvm_pilot.drivers.fake import FakeDriver

    d = FakeDriver(powered=True)
    d.hard_cycle()
    names = [a[0] for a in d.actions]
    assert names == ["power_off_hard", "power_on"]


def test_hard_cycle_explicit_delays_override_class_attr(monkeypatch):
    # An explicit delay wins over the class attribute; assert the mixin passes it
    # to time.sleep without actually sleeping.
    import kvm_pilot.drivers.base as base
    from kvm_pilot.drivers.fake import FakeDriver

    slept: list[float] = []
    monkeypatch.setattr(base.time, "sleep", lambda s: slept.append(s))
    FakeDriver(powered=True).hard_cycle(off_delay=1.5, on_delay=2.5)
    assert slept == [1.5, 2.5]
