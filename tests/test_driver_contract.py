"""Cross-driver contract tests — enforce the first-class-driver standard.

These codify requirements that every driver must meet regardless of transport, so
a new driver can't regress the fleet-wide quality bar. When you add a driver, add
it here. The per-driver *shape* assertions live in each driver's own test module
(e.g. ``test_amt_driver.py::test_firmware_info_has_vendor_product``); this module
is the shared guard that a driver didn't simply forget a contract method.

See ``docs/plugin-development.md`` for the full Definition-of-Done.
"""

from __future__ import annotations

import inspect

import pytest

from kvm_pilot.drivers.amt import AmtDriver
from kvm_pilot.drivers.base import Capability, detect_capabilities
from kvm_pilot.drivers.ipmi import IpmiDriver
from kvm_pilot.drivers.redfish import RedfishDriver

# The out-of-band management drivers (not the kvmd/PiKVM family, which is
# transport-identical). Extend this list when a new OOB driver lands.
OOB_DRIVERS = [AmtDriver, IpmiDriver, RedfishDriver]


@pytest.mark.parametrize("cls", OOB_DRIVERS, ids=lambda c: c.__name__)
def test_normalized_firmware_identity_method(cls):
    """Every OOB driver must implement ``get_firmware_info`` — the run ledger and
    firmware registry join on its ``vendor``/``product``. A driver without it
    records device identity as ``fake/fake`` (the historical test-report bug)."""
    assert hasattr(cls, "get_firmware_info"), (
        f"{cls.__name__} must implement get_firmware_info() returning vendor+product"
    )


@pytest.mark.parametrize("cls", OOB_DRIVERS, ids=lambda c: c.__name__)
def test_from_config_constructor(cls):
    """Every driver must be buildable from a HostConfig via from_config()."""
    assert hasattr(cls, "from_config"), f"{cls.__name__} must implement from_config()"


def test_amt_hid_accepts_cli_signatures():
    """A driver advertising HID must accept the richer CLI/MCP call signatures
    (``type_text(text, slow=)``, ``mouse_click(button, double=)``,
    ``mouse_move_percent``/``mouse_move_pixels``) — the P0 bug where a minimal-HID
    driver tracebacked through the CLI. Checked structurally here so any future
    minimal-HID driver is held to the same bar."""
    drv = AmtDriver("10.0.0.1", "admin", "secret")
    assert Capability.HID in detect_capabilities(drv)
    sig = inspect.signature(drv.type_text)
    assert "slow" in sig.parameters
    assert "double" in inspect.signature(drv.mouse_click).parameters
    for extra in ("mouse_move_percent", "mouse_move_pixels"):
        assert callable(getattr(drv, extra, None)), f"HID driver should expose {extra}"
