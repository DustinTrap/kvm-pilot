"""AmtDriver against a real (or independently-emulated) Intel AMT endpoint — #211.

Marked ``integration`` and skipped unless an AMT WS-Man endpoint is advertised
(see the ``amt_endpoint`` fixture). The point, as with the sushy-tools Redfish and
``ipmi_sim`` IPMI legs, is *independence*: driving a WS-Man stack we didn't write
(a live Management Engine, or an independent emulator such as MeshCommander's
``amtsim`` / Open-AMT ``rpc-go`` reference) so a spec assumption shared by our
driver and our in-process fake can't hide a bug.

There is no self-contained, pip-installable AMT emulator to stand up here the way
``sushy-emulator``/``ipmi_sim`` are, so this leg is purely env-driven and stays
skipped by default (macOS/CI have no ME). Point it at hardware or an emulator via
``KVM_PILOT_AMT_HOST`` [+ ``_PORT`` / ``_USER`` / ``_PASSWD`` / ``_TLS``].

Only *read-only* verbs run here — capabilities, identity, power state, boot-option
feature-detection. Power flips, boot overrides, and KVM/SOL enablement are
destructive on a real platform and are covered by the unit tests
(``tests/test_amt_driver.py``) against the in-process WS-Man/RFB emulators.
"""

from __future__ import annotations

import os

import pytest

from kvm_pilot.drivers.amt import AmtDriver
from kvm_pilot.drivers.base import BootConfig, Power, SystemInfo
from kvm_pilot.safety import deny_all

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def amt_endpoint() -> dict:
    """Connection params for an external AMT WS-Man endpoint (real ME or emulator).

    Env-driven (mirrors ``ipmi_bmc``): skips when unset so the default suite stays
    hermetic. ``KVM_PILOT_AMT_TLS=1`` selects HTTPS (16993) over plain HTTP (16992).
    """
    host = os.environ.get("KVM_PILOT_AMT_HOST")
    if not host:
        pytest.skip(
            "external AMT endpoint unavailable: set KVM_PILOT_AMT_HOST (+ _PORT/_USER/"
            "_PASSWD/_TLS) to a real Management Engine or an independent WS-Man emulator"
        )
    tls = os.environ.get("KVM_PILOT_AMT_TLS", "").lower() in ("1", "true", "yes")
    return {
        "host": host,
        "port": int(os.environ.get("KVM_PILOT_AMT_PORT", "16993" if tls else "16992")),
        "user": os.environ.get("KVM_PILOT_AMT_USER", "admin"),
        "passwd": os.environ.get("KVM_PILOT_AMT_PASSWD", ""),
        "tls": tls,
    }


def _driver(ep: dict) -> AmtDriver:
    # deny_all: a hard guarantee this read-only leg can never fire a destructive op.
    return AmtDriver(
        ep["host"], ep["user"], ep["passwd"], port=ep["port"], tls=ep["tls"], confirm=deny_all
    )


def test_capabilities_match_the_amt_set(amt_endpoint):
    d = _driver(amt_endpoint)
    assert isinstance(d, Power | SystemInfo | BootConfig)


def test_identity_reads_back_from_wsman(amt_endpoint):
    # An independent WS-Man stack answered through AmtDriver -> urllib digest ->
    # SOAP, and our CIM/AMT parsers read a real identity + power state back.
    info = _driver(amt_endpoint).get_info()
    assert info["power_state"] in ("on", "off")
    assert info["amt_version"] is not None or info["manufacturer"] is not None


def test_power_state_is_readable(amt_endpoint):
    assert isinstance(_driver(amt_endpoint).is_powered_on(), bool)


def test_boot_options_feature_detect(amt_endpoint):
    # AMT exposes only the forced sources it supports; usb/diag are never allowable.
    opts = _driver(amt_endpoint).get_boot_options()
    assert "pxe" in opts["allowable"]
    assert "usb" not in opts["allowable"]
    assert opts["persistent"] is False  # AMT overrides are single-use only
