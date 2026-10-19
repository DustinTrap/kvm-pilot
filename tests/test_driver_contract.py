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


def _unanswering(cls, monkeypatch):
    """``cls`` built against a management plane that does not answer."""
    import socket
    import types

    from kvm_pilot.config import HostConfig

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    if cls is IpmiDriver:  # shells out to ipmitool: fake its no-BMC exit
        import kvm_pilot.drivers.ipmi as ipmi_mod

        monkeypatch.setattr(ipmi_mod.shutil, "which", lambda _name: "/usr/bin/ipmitool")
        monkeypatch.setattr(ipmi_mod.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
            returncode=1, stdout="", stderr="Error: Unable to establish IPMI v2 / RMCP+ session"))
    cfg = HostConfig(host="127.0.0.1", port=port, amt_port=port, scheme="http", timeout=1.0)
    return cls.from_config(cfg, confirm=lambda *_: False)


@pytest.mark.parametrize("cls", OOB_DRIVERS, ids=lambda c: c.__name__)
def test_get_info_raises_when_the_management_plane_does_not_answer(cls, monkeypatch):
    """The healthcheck's api-reachable check and ``access_paths`` (out-of-band
    power included) read "get_info() returned" as "the plane answers". So get_info
    may be best-effort per FIELD, but must RAISE when nothing answers: returning
    blanks is how #293 reported a dark AMT host as healthy."""
    from kvm_pilot.errors import KVMPilotError

    with pytest.raises(KVMPilotError):
        _unanswering(cls, monkeypatch).get_info()


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
