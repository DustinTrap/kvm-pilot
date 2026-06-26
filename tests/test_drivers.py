"""Capability-protocol conformance for the driver layer (step 1)."""

from __future__ import annotations

from kvm_pilot import KVMClient
from kvm_pilot.drivers.base import (
    GPIO,
    HID,
    Capability,
    Events,
    Power,
    SystemInfo,
    Video,
    VirtualMedia,
    detect_capabilities,
)

ALL_PROTOCOLS = (SystemInfo, Power, HID, Video, VirtualMedia, GPIO, Events)


def make_client() -> KVMClient:
    # TEST-NET-1 address (RFC 5737); constructing a client performs no network I/O.
    return KVMClient("192.0.2.1")


def test_pikvm_client_satisfies_every_capability_protocol() -> None:
    client = make_client()
    for proto in ALL_PROTOCOLS:
        assert isinstance(client, proto), proto.__name__


def test_capabilities_reports_the_full_set() -> None:
    assert make_client().capabilities() == set(Capability)


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
